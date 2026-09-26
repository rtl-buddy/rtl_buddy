---
description: Run OpenROAD gate-level power analysis from synthesis or P&R outputs using static, synthetic, SAIF, or VCD activity.
---

# Power Analysis

`rb power` runs OpenROAD `report_power` on a mapped design and reports total, internal, switching, and leakage power. FPGA runs report Vivado power directly; they do not use this command.

## Choose the design source

| Source | Input | Timing and parasitics | Required upstream runs |
| --- | --- | --- | --- |
| `netlist-source: synth` | `synth_netlist.v` | User SDC, no wire parasitics or clock tree | `rb synth` |
| `netlist-source: pnr` | `<top>.routed.odb` | Routed SDC, CTS, and the P&R run's extracted SPEF — or global-routing parasitic estimates when there is none | `rb synth`, then `rb pnr` |

The default `synth` source is useful for early leakage and activity comparisons but underestimates switching because it has no routed wire capacitance. Use `pnr` for a more representative post-route estimate.

If the routed ODB is missing, rerun `rb pnr`.

### Extracted parasitics

When the P&R run's PDK sets [`rcx-rules`](pnr.md#tune-the-process-dependent-steps), `rb pnr` writes an OpenRCX-extracted `<top>.routed.spef` beside the ODB, and a `netlist-source: pnr` power run reads it with `read_spef` after the routed SDC, in place of `estimate_parasitics -global_routing`. Without one — including the template's Nangate45 runs, which leave the key unset — the ODB handoff and the global-route estimate are used exactly as before.

A SPEF is read only when the P&R run that wrote the ODB vouches for it: its `pnr.tcl` must contain a `write_spef` command, and the SPEF must be no older than that `pnr.tcl`, which every `rb pnr` rewrites at the start of every run. A SPEF that fails either test is left unread with a `power.spef_rejected` WARNING naming the reason, and the run falls back to the estimate. That covers the case `rb pnr`'s own clearing cannot: an rtl_buddy that predates the SPEF reruns P&R and leaves the previous run's extraction beside a fresh ODB.

Which one was used is the run's `parasitics` result field — `spef` or `estimated`, absent on a `synth` source — shown in the summary's Parasitics column, logged as `power.parasitics`, and part of the model's options digest, so one ODB measured both ways is two experiments.

On the project template's flat sky130hd pipeclean (`demo_tiny_alu_subsys_sky130_flat_power`, static mode, default activity), switching power rose from 372 µW on the estimate to 467 µW on the extracted SPEF (+25 %; total 3.47 → 3.56 mW), with the clock network 269 → 315 µW and the registers 60 → 99 µW, while internal power (3.08 mW) and leakage (9.5 µW) — which do not depend on wire load — stayed put. The same P&R run's setup WNS went from +3.61 ns to +2.68 ns; nothing before extraction changed, and area and cell count are identical. The direction is the expected one: the global-route estimate prices wires as per-layer R and C along routing guides, while OpenRCX extracts the detailed routes themselves, vias and coupling capacitance included, and the estimate also skipped the segments it could not find a route for (`EST-0026 Missing route to pin` on seven reset pins) where extraction does not.

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

Paths resolve from `power.yaml`. A synth-source run requires `synth`, `synth-path`, and `constraints`. A P&R-source run requires `pnr` and `pnr-path`; it uses the routed SDC unless `constraints` overrides it. `phys-run` is optional and names the synthesis run this one publishes its half of the physical model beside; see [Pair the model with a synthesis run](#pair-the-model-with-a-synthesis-run).

See [YAML Formats: power.yaml](../reference/yaml.md#poweryaml) for all fields.

## Give hard macros a library

The `platform` corner characterises the standard cells and nothing else. A hard macro — an SRAM, a PLL, a hardened partition — gets its Liberty from the run that placed it, through that run's `lib-paths`, and a power analysis that read only the platform corner had no library for it at all. The macro was still in the design and still in `power_instances.rpt`, contributing exactly zero:

```text
Macro                  0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%
```

The power run inherits those libraries from the run it references, so the ordinary case needs no new configuration:

| `netlist-source` | Inherited | Why |
| --- | --- | --- |
| `pnr` | the P&R entry's `lib-paths` | The routed ODB already carries every master the router placed, so no LEF is read |
| `synth` | the synthesis entry's `lib-paths` and `lef-paths` | `read_verilog` and `link_design` build the database out of LEF masters |

`lib-paths` on the `power.yaml` entry adds to the inherited list, for a library only the analysis needs — a corner the upstream run was not routed against, say:

```yaml
runs:
  - name: demo_power_macro
    netlist-source: pnr
    pnr: demo_pnr
    pnr-path: ../../pnr/demo/pnr.yaml
    platform: sky130hd_tt
    lib-paths: ["../../pdk/sram/sram_TT_1p8V_25C.lib"]
```

Paths resolve from `power.yaml`, as every other path on the entry does. The inherited libraries are read first and the run's own after them, so a macro library never shadows a standard cell; a library named on both sides is read once. A configured file that is not on disk fails the run before OpenROAD is launched, rather than leaving a diagnostic in `power.log` and the macro back at zero.

## Read the unpowered-cell warning

An instance whose master no Liberty covers reports `0.00e+00` in all four columns, and nothing in the report distinguishes that from a cell that genuinely burns nothing. A run that finds one says so:

```text
power run "demo_power_macro": 1 instance(s) have no Liberty power data and report 0 W — sram_32x256.
```

The same run's description carries the qualifier, and `--machine` carries `unpowered_cells`, `unpowered_cell_count` and `unpowered_instance_count`. All three are absent from a run that found nothing, so a consumer reading `unpowered_cells` is reading a total that does not cover the whole design.

The verdict does not change. The watts reported are a real measurement of everything that had a library, and a design that carries a LEF-only macro on purpose — an ORFS `fakeram45`, a placeholder — would otherwise have no way to pass. Gate on `unpowered_cell_count` where the flow signs off on power.

A cell is reported only when all three hold: its master is declared in none of the Liberty files the generated script read, its total power is exactly zero, and it is not one of the PDK's `fill-cells`. The second keeps an unusual Liberty spelling from turning a whole standard-cell library into a warning; the first keeps a flop that really does sit at zero out of it; the third keeps the fill instances `filler_placement` left in the routed database out of it, which on the sky130 pipeclean is 26 076 instances against one SRAM. Which instances they are is in `power_instances.rpt` and in `phys-model.json`, with the cell beside each.

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

The analysis runs on one OpenROAD thread unless the run sets `threads:` — a positive integer, or `auto` for the CPUs of the current allocation. The value is validated, clamped, emitted and recorded as `openroad_threads` exactly as for P&R; see [OpenROAD threads](pnr.md#openroad-threads).

## Interpret results

The summary identifies the selected design source and resolved activity source, then reports total, internal, switching, and leakage power with readable SI scaling.

A run passes when OpenROAD exits 0, emits no `[ERROR ...]` line, and produces a parseable `Total` row in `power.rpt`. It skips when filtered by `reglvl` or when its tool has no registered backend.

## Pair the model with a synthesis run

A complete physical model needs both halves — the synthesis' per-module cells and area, the power run's per-instance watts — and the two halves meet in one artefact directory. Each run publishes into its own by default, so a merged model is what a power run named after the synthesis it reads *and* configured in the same directory produces. Split the suites into `synth/` and `power/`, or rename either run, and each half lands in a directory of its own: `rb phys instance` exits 2 on the synthesis' directory and `rb phys module` reports no modules in the power run's, with nothing saying why.

`phys-run` says the pairing out loud:

```yaml
runs:
  - name: demo_power_static
    synth: demo_synth_nangate45
    synth-path: ../../synth/demo/synth.yaml
    phys-run: demo_synth_nangate45
```

The value names a run in the `synth.yaml` that `synth-path` already points at, and the power half is published into that run's `artefacts/<run>/` — the directory the synthesis writes its own half into. It is a run name, not a path: the directory is derived, so moving the synthesis suite moves the pairing with it. Everything else this run writes — the log, the reports, its copy of the netlist — stays in the power run's own artefact directory, and the manifest names them there.

The named directory need not exist yet. A power run may land first and the synthesis fill the other half later, which is what publishing into a directory chosen rather than inherited is for. A `phys-run` that names no entry in the referenced `synth.yaml`, or that resolves outside the project because `synth-path` reaches into another checkout, fails the run as any other configuration error does. `phys-run` requires `netlist-source: synth`: a `pnr`-source run records no netlist hash, so its half can never merge with a synthesis' and pointing it at one would replace the module rows rather than complete them.

One directory holds one power half. Two power runs that name the same `phys-run` — a static and a dynamic analysis of one design, say — publish into it in turn, and the model there describes whichever ran last; a failed rerun of either withdraws the rows that are there. Leave `phys-run` off the runs whose breakdowns are to be kept apart, and they publish into their own directories as before.

The manifest's header names the run whose publication it is, so a shared directory reads as the synthesis or as the power run depending on which published last, and that is the name `rb phys runs` lists. Each half's own block names its producer either way. Changing or removing `phys-run` leaves the previous directory's power half where it is, as renaming any run leaves its artefact directory behind: re-run the analysis, or delete the directory.

**Naming the pairing does not make it.** The netlist sha256 both producers record is still the whole of the merge gate, and it decides exactly as before. What changes is that a refusal is no longer silent: a run given a `phys-run` whose module rows were counted off a netlist it did not read logs a warning and publishes its half alone. Re-run the synthesis and the power analysis over one netlist to pair them. See [Read a model with only one half](phys.md#read-a-model-with-only-one-half).

## Inspect artefacts

Outputs land under `<power-dir>/artefacts/<run>/`:

| File | Purpose |
| --- | --- |
| `power.tcl` | Generated OpenROAD script |
| `power.log` | OpenROAD output |
| `power.rpt` | Raw `report_power` report |
| `power_netlist.v` | This run's copy of the upstream netlist, the file OpenROAD reads |
| `power_instances.rpt` | Raw `report_power -instances` report, one line per leaf instance |
| `power_instances.cells` | Instance path to Liberty cell, the module column that report lacks |
| `phys-model.json` | Physical model — per-instance rows plus the design totals |
| `phys-manifest.json` | Which physical artefacts this run produced, and where |
| `phys-publish.lock` | Mutex held while that pair is rewritten; empty between publishes |

Query the model with `rb phys`; see [Physical Metrics](phys.md).

`phys-model.json` and `phys-manifest.json` are the exception to the table above: with `phys-run` set they are written into the synthesis run's artefact directory instead, and everything else stays here. See [Pair the model with a synthesis run](#pair-the-model-with-a-synthesis-run).

The per-instance half is a by-product, never a gate: the design totals are parsed and reported before it is read, and the hierarchy walk that produces it runs inside a Tcl `catch`. An OpenSTA that cannot produce it costs the model its `instances` block — `null`, with a warning — and the run still reports `PASS`. A `rb synth` run publishing into the same artefact directory for the same top fills the model's per-module half rather than replacing it. Either half travels only when both runs recorded the same netlist hash: this run keeps the per-module rows already there when the netlist it read is the one they were counted off, and a *later* synthesis keeps these per-instance rows when the netlist it writes hashes equal to the one this run read. The two flows are still not symmetric, but the check is — the ordinary `rb synth` then `rb power` pair passes it because a power run reads exactly what the synthesis wrote, while a rebuild between the two runs fails it in whichever direction publishes second. A `netlist-source: pnr` run reads the routed database rather than a netlist and records no such hash, so it inherits no per-module rows and its own rows are never carried forward by a synthesis. The netlist a `netlist-source: synth` run measures is copied into its own artefact directory as `power_netlist.v` first, and it is that copy OpenROAD reads and that copy the hash identifies. The bytes measured and the bytes named are therefore the same file, which no other command writes: a synthesis landing in the upstream directory mid-analysis is caught rather than recorded as a match. The manifest names that copy alongside the hash, so a result read back from an archive reaches the netlist the numbers were measured over and can re-check the hash against it; a `netlist-source: pnr` run names none. The copy is removed before each run and again if the run fails, like the reports beside it.

An FPGA run and a power run must not share a name within one suite: both own `artefacts/<name>/power.rpt` and the second to run overwrites the first. Ownership cannot be told apart by filename, so rtl_buddy does not try — give them distinct names.

On failure, inspect `power.log` for tool and input errors, then `power.rpt` for missing or malformed totals. `power.rpt` is deleted before each run and again if the run fails after writing it, so a run that never got as far as `report_power` — or that reached it and then failed — leaves none — read `power.log` in that case rather than an earlier run's numbers. A run that cannot find its backend tool is the exception: it deletes nothing, because a machine without the tool never ran it and has no business removing what a machine that has it produced.
