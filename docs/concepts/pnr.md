---
description: Run OpenROAD place-and-route from a mapped synthesis result, configure a physical platform, and inspect timing, DRC, GDS, and layout artefacts.
---

# Place-and-Route

`rb pnr` takes a technology-mapped `rb synth` result, runs OpenROAD placement, clock-tree synthesis and routing, and reports area, timing and DRCs. It can also stream the layout to GDS and PNG with KLayout, harden a block for reuse in a larger design, and keep stage checkpoints.

## Install the tools

OpenROAD 25Q1 or newer must be on `PATH` or set in `cfg-pnr-tools`. An older version logs a warning and the run continues, but that combination is not validated. On macOS, build from source with the template's `tools/openroad/BUILD_OSX.md`.

KLayout (`brew install --cask klayout`) is optional and only needed for `--gds` and `--png`. Without it those steps are skipped and the run does not fail. Install it later and [export the saved result](#export-a-saved-result) instead of rerunning P&R.

## Define a P&R run

```yaml
rtl-buddy-filetype: pnr_config

runs:
  - name: demo_pnr_nangate45
    desc: Nangate45 typical-corner P&R
    tool: openroad
    synth: demo_synth_nangate45
    synth-path: ../../synth/demo/synth.yaml
    constraints: ../../synth/demo/constraints.sdc
    platform: nangate45_typ
    floorplan:
      utilization: 0.55
      aspect: 1.0
      core-margin: 2.0
    reglvl: 1000
```

Paths resolve from `pnr.yaml`. The named synthesis must already have produced `artefacts/<synth>/synth_netlist.v`. The top module comes from that synthesis entry, and Liberty and LEF assets come from the platform. Only `tool: openroad` is supported; any other value reports `SKIP`. All fields are in [YAML Formats: pnr.yaml](../reference/yaml.md#pnryaml).

## Run P&R

```bash
rb pnr --list -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml -l 1000
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png --gds-mode strict
```

`--png` and `--gds-mode` imply `--gds`. KLayout runs after a successful OpenROAD run. In the default `preview` mode a KLayout failure logs a warning and leaves the P&R verdict unchanged. In `strict` mode an undelivered export fails the run; see [Stream-out completeness](#stream-out-completeness).

## Flow steps

The generated `pnr.tcl` runs these steps in order:

1. Source `platform-tcl` when set, read Liberty, LEF, the synthesis netlist and the SDC, set the design's max fanout when `max-fanout` is set, then source `layer-rc-tcl` when set.
2. Initialize the floorplan, create routing tracks and run `insert_tiecells` for the PDK's `tie-hi` and `tie-lo` ports, one tie cell per constant net.
3. Place macros, insert tap and endcap cells when `tapcell-tcl` is set, build the power grid and place the IO pins.
4. Run global placement, with `-reference_hpwl` when `placement.reference-hpwl` is set, then `repair_tie_fanout` for each tie port, which gives every constant-driven load its own tie cell `placement.tie-separation` microns away (default 0).
5. Run `repair_design`, legalization, clock-tree synthesis (with `-apply_ndr` when `cts-apply-ndr` is set), setup repair when `post-cts-setup-repair` is set, hold repair and a final legalization.
6. Route globally, after `set_global_routing_layer_adjustment` when `routing-layer-adjustment` is set, and in detail, insert fill, extract parasitics when `rcx-rules` is set, then write reports and outputs.

A tie port the PDK leaves unset gets neither tie step.

## Interpret results

The summary reports cell count, design area, setup and hold WNS, and the number of non-empty DRC report lines. Positive slack meets timing and zero DRC lines indicate a clean route. `wns_setup_ps`, `wns_hold_ps`, `tns_ps` (setup) and `tns_hold_ps` are converted to picoseconds from the Liberty `time_unit`; see [Synthesis: Configure tools and the PDK](synthesis.md#configure-tools-and-the-pdk).

Three result fields count cells; compare experiments on `routed_cell_count`:

- `cell_count`, the summary's Cells column, is the input netlist's instance count at the floorplan. It leaves out every cell the flow adds.
- `routed_cell_count` is the finished design's instances, including tie, repair, clock-tree and hold buffers, excluding physical-only cells.
- `physical_cell_count` is the physical-only cells: masters of LEF class `CORE SPACER`, `CORE WELLTAP` or `ENDCAP*`, or matching a PDK `fill-cells` pattern. A decap counts only when its class is `CORE SPACER` or it is listed in `fill-cells`; sky130's `decap_*` cells are class `CORE`, so they count as routed unless listed.

The flow prints the last two after `>>> Final reports` as `RB-CELL-COUNT: routed <n> physical <m>`. If counting fails, both fields are absent and the run still passes.

A run passes when OpenROAD exits 0 with no `[ERROR ...]` line and, under `gds-mode: strict`, the requested export was delivered complete. It skips when `reglvl` filters it out or `tool:` is unsupported. Timing violations and DRC counts are metrics only; gate signoff on them in your project.

On a multi-corner platform, `wns_setup_ps`, `wns_hold_ps`, `tns_ps` and `tns_hold_ps` are the worst across corners. The result also names the `worst_setup_corner` and `worst_hold_corner` and lists each corner's own four values under `corners`, so setup at the slow corner and hold at the fast corner can be read separately; `pnr.log` has them after `>>> Per-corner timing`.

## Inspect artefacts

Outputs land under `<pnr-dir>/artefacts/<run>/`.

| File | Purpose |
| --- | --- |
| `pnr.log`, `pnr.tcl` | OpenROAD output and generated flow |
| `<top>.def`, `<top>.routed.v`, `<top>.routed.sdc` | Routed DEF, post-route netlist and constraints |
| `<top>.routed.odb` | OpenROAD database read by post-P&R `rb power` |
| `<top>.routed.spef` | Extracted parasitics; only when the PDK sets `rcx-rules` |
| `timing.rpt` | Worst-path timing across all corners |
| `route.drc.rpt`, `route.maze.log` | DRC summary and detailed-route log |
| `<top>.gds`, `<top>.png`, `klayout.*.log` | Optional KLayout outputs and logs |
| `export.provenance.json` | What the last `rb pnr-export` read and produced |
| `checkpoints/`, `abstract/` | Optional [stage checkpoints](#keep-stage-checkpoints) and [hardened-block abstract](#harden-a-block) |

Each run deletes the previous run's outputs first, and a run that fails after writing the routed database removes it again, so `rb power` never reads a stale one. `pnr.log` and `pnr.tcl` are kept from a failed run.

On failure, read `pnr.log`. If only KLayout failed, read the matching `klayout.*.log`, fix the installation and rerun with `--gds` or `--png`.

## Configure the physical platform

Define PDK files once under `cfg-pdks`. Select a process and corner for P&R under `cfg-pnr-platforms`:

```yaml
cfg-pnr-platforms:
  - name: nangate45_typ
    pdk: nangate45
    corner: typ
    cts-buffer: BUF_X4
    routing-layers:
      signal: metal2-metal8
      clock: metal4-metal8
```

The PDK entry supplies Liberty, technology and macro LEF, cell GDS, site and other cell names. See [Synthesis: Configure tools and the PDK](synthesis.md#configure-tools-and-the-pdk) and the [root config schema](../reference/yaml.md#root_configyaml).

## Sign off at several corners

`corners:` in place of `corner:` analyses a list of the PDK's corners together. `rb pnr` and `rb power` read it; `rb synth` stays single-corner.

```yaml
cfg-pdks:
  - name: sky130hd
    corners:
      tt: pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
      ss: pdk/sky130hd/lib/sky130_fd_sc_hd__ss_n40C_1v40.lib

cfg-pnr-platforms:
  - name: sky130hd_mc
    pdk: sky130hd
    corners: [tt, ss]    # the first entry is the primary corner
```

- All corners run in one OpenROAD session, so repair and the final worst-slack and TNS reports see every corner. Clock-tree synthesis uses the primary (first) corner.
- `corner` and `corners` are mutually exclusive. The list must be non-empty, and each name must be declared in the PDK, appear once, and use only letters, digits, `_`, `.` and `-`. A one-entry list is the same run as `corner:`. A platform that sets neither uses the PDK's first corner.
- A macro with one Liberty in `lib-paths` is read into every corner and keeps the timing of the corner it was characterised at, so a slow corner can fail because of the macro (for example `RSZ-0090` from a TT SRAM Liberty). Give such a macro a Liberty per corner. OpenSTA logs `STA-1140 library ... already exists` once per extra corner; that is expected.

## Tune the process-dependent steps

Placement, clock-tree and power-grid steps default to values calibrated for Nangate45. Another process declares its own. Every key below is optional.

```yaml
cfg-pdks:
  - name: sky130hd
    # ...
    placement:
      density: 0.55        # default 0.7
      padding: 2           # default 1, in sites
      macro-halo: 30.0     # default 20.0, in microns
    dont-use-cells: ["sky130_fd_sc_hd__probe*"]
    pdn-config: pdk/sky130hd/pdn.tcl
    rcx-rules: pdk/sky130hd/rcx_patterns.rules

cfg-pnr-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    cts-buffer: [sky130_fd_sc_hd__clkbuf_4, sky130_fd_sc_hd__clkbuf_8]
    placement:
      density: 0.6         # this platform only; padding stays the PDK's 2
```

- **`placement.density`** is the global-placement target utilization, above 0 and at most 1. **`placement.padding`** is cell padding in sites. A platform overrides either one field by field.
- **`placement.macro-halo`** (default 20.0 µm) is the channel kept between macros and between a macro and each core edge. Below about 19 µm on sky130hd, `pdngen` fails with `PDN-0179`. Raise it for a coarser grid; lower it only when macros do not fit.
- **`placement.macro-cell-halo`** (default 1.0 µm) keeps standard cells off macros with a hard blockage. Without it, the router can report a Metal Spacing violation at a pin on a hardened block's edge. `0` disables it.
- **`placement.tie-separation`** (default 0 µm) is how far from its load each per-load tie cell is placed; see [Flow steps](#flow-steps).
- **`placement.reference-hpwl`** (positive number, unset by default) passes `-reference_hpwl` to `global_placement`. Unset, the placer's density-penalty controller derives its reference HPWL from the design size, which on a large design spreads cells less and ends routability inflation early, so global route can overflow. A fixed value such as `446000000` spreads cells further and can clear the overflow.
- **`cts-buffer`** takes a name or a list. With a list, the first entry is the root buffer.
- **`cts-sink-clustering`** (default `true`) passes `-sink_clustering_enable` to CTS. Set it to `false` when CTS fails with `CTS-0080 Sink not found`, as it can on coincident clock pins or a large ASAP7 clock tree.
- **`cts-apply-ndr`** (unset by default) passes `-apply_ndr` (`none`, `root_only`, `half` or `full`) to CTS; unset keeps OpenROAD's default, `half`. Set `none` when the global router detours clock nets that carry non-default rules to several times their Manhattan length, which shows as large hold violations and `GRT-0273 Disabled NDR` in the log.
- **`max-fanout`** (positive integer, unset by default) adds `set_max_fanout <n> [current_design]` after each `read_sdc` in `rb pnr` and `rb power`, replacing a design-level limit from the SDC. When neither the Liberty nor the SDC sets a limit, `repair_design` uses 50 and buffers every larger net; on a library without one, such as SKY130 HD, that costs setup timing. A large value such as `100000` stops repair buffering for fanout alone.
- **`post-cts-setup-repair`** (default `false`) runs `repair_timing -setup` after CTS, before hold repair. Turn it on when the post-CTS netlist misses setup; it adds buffers and resizes cells, so QoR changes.
- **`routing-layer-adjustment`** (0 to 1, unset by default) withholds that fraction of each signal layer's capacity from the global router (`set_global_routing_layer_adjustment`, ORFS `ROUTING_LAYER_ADJUSTMENT`, 0.25 on ASAP7). Raise it when detailed routing ends with DRCs in congested areas; unset leaves the router's default.
- **`pdn-config`** is a path, resolved from `root_config.yaml`, to a Tcl snippet that declares the power grid (`add_global_connection`, `set_voltage_domain`, `define_pdn_grid`, `add_pdn_stripe`, `add_pdn_connect`). The flow sources it after macro placement and calls `pdngen` itself, so the snippet must not. Unset means no power grid.
- **`dont-use-cells`** and **`rcx-rules`** are described below, and the platform Tcl hooks under [Source platform Tcl hooks](#source-platform-tcl-hooks).

## Exclude cells with dont-use-cells

`dont-use-cells` is a list of cell names or patterns on the PDK, read by both `rb pnr` and `rb synth`. P&R applies it before the floorplan, so no placement, repair or CTS step chooses a listed cell. Synthesis passes it to Yosys and, on the OpenROAD backend, to resynthesis.

- Write one pattern per entry. Only `*` and `?` are allowed. An entry with whitespace or a Tcl metacharacter (`[ ] { } $ " \ ;`) is rejected when the config loads.
- A synth or P&R platform can add its own list, appended to the PDK's. A platform's list reaches only its own flow; the PDK's reaches both.

With any cell excluded, the flow checks the routed design after detailed routing, before any output is written. It prints each offending instance to `pnr.log` as `RB-DONT-USE-VIOLATION: <instance> <master> <pattern>` and fails the run, which catches a cell the synthesis netlist already instantiated. An `xfail:` marker can excuse it.

A pattern that matches no Liberty cell excludes nothing. OpenROAD reports `STA-0122 cell '<pattern>' not found` and rb logs a warning naming the pattern.

## Extract parasitics with rcx-rules

`rcx-rules` is a path, resolved like `pdn-config`, to an OpenRCX extraction-rules file (ORFS `RCX_RULES`). Its `LayerCount` must match the technology LEF's routing stack.

With it set, the flow extracts parasitics after fill insertion, writes `<top>.routed.spef`, and times the final reports on that SPEF instead of the global-route estimate. The summary's WNS and TNS come from the extracted parasitics, and a [`netlist-source: pnr` power run](power.md#extracted-parasitics) reads the same SPEF. With the key unset there is no extraction.

## Source platform Tcl hooks

Four optional PDK paths, resolved like `pdn-config`, name Tcl scripts the flow sources at the step where OpenROAD-flow-scripts (ORFS) sources its own. A platform taken from ORFS, such as ASAP7, needs them.

| Key | ORFS variable | Sourced |
| --- | --- | --- |
| `platform-tcl` | `PLATFORM_TCL` | Before the first `read_liberty`, so `suppress_message` lines cover the Liberty reads |
| `layer-rc-tcl` | `SET_RC_TCL` | After `read_sdc`, so `repair_design`, CTS, hold repair and the final reports see wire RC |
| `tracks-tcl` | `MAKE_TRACKS` | After `initialize_floorplan`, instead of the bare `make_tracks` |
| `tapcell-tcl` | `TAPCELL_TCL` | After macro placement and the macro keep-outs, before the power grid |

```yaml
cfg-pdks:
  - name: asap7
    # ...
    platform-tcl: pdk/asap7/liberty_suppressions.tcl
    layer-rc-tcl: pdk/asap7/setRC.tcl
    tracks-tcl: pdk/asap7/make_tracks.tcl
    tapcell-tcl: pdk/asap7/tapcell.tcl
```

- An unset key leaves the flow as it is: `make_tracks` with the technology LEF's default tracks, no taps, and the LEF's layer RC, which is zero on ASAP7.
- Each file is sourced as written, so it must not depend on ORFS environment variables. ORFS' ASAP7 `openRoad/tapcell.tcl` takes both its tap and endcap master from `$::env(TAP_CELL_NAME)` and its macro halo from `$::env(MACRO_ROWS_HALO_X)` and `$::env(MACRO_ROWS_HALO_Y)`; copy it with those values filled in.
- A configured file missing from disk fails the run at `setup`, naming the key.
- `rb power` sources `platform-tcl` before its Liberty reads. The layer RC is session state that the routed ODB does not keep, so a [`netlist-source: pnr` power run](power.md#extracted-parasitics) also sources `layer-rc-tcl` after `read_sdc`.
- The hooks are inputs to stage checkpoints and to a [hardened block's](#harden-a-block) abstract, so changing one makes the abstract stale.

## Macro placement

A netlist that instantiates hard macros, such as an SRAM or a partition hardened by an earlier run, gets automatic macro placement before the power grid is built. To use OpenROAD's placer instead, see [RTL-MP macro placement](#rtl-mp-macro-placement).

- The packer sorts macros tallest first, then widest, and packs them left to right into rows starting at the core's lower-left corner, or at the corner [`floorplan.macro-anchor`](#floorplan-controls) names.
- It keeps `placement.macro-halo` between neighbours and at every core edge, and snaps each origin to the standard-cell site grid.
- Placed macros are `FIRM`; global and detailed placement leave them where they are.

When macros do not fit, the run fails with `N macros do not fit the W x H um floorplan core`, followed by the largest footprint, the macro and core areas, and the smallest core at the run's aspect ratio that would fit. Lower the floorplan utilization, change its aspect ratio, or lower `placement.macro-halo`.

## RTL-MP macro placement

`floorplan.macro-placement: rtl-mp` (default `pack`) hands macros to OpenROAD's hierarchical macro placer, `rtl_macro_placer`, instead of the packer:

```yaml
    floorplan:
      utilization: 0.45
      macro-placement: rtl-mp
```

RTL-MP places macros by connectivity and wirelength and may rotate them. Macros end up `LOCKED` on the track grid, and reports go to `artefacts/<run>/rtlmp/`. Use it for a flat netlist with many memories or when timing depends on macro placement; on the template's sky130hd assembly it gave about 0.8 ns better setup slack than the packer.

- It keeps `placement.macro-halo` around every macro and keeps macros out of every placement blockage, including `soft` and `partial` ones that the packer lets macros sit on.
- [`macro-cell-halo`](#tune-the-process-dependent-steps) and the power grid apply afterwards as usual.
- Setting `macro-anchor` with `rtl-mp` is a configuration error. Its result also changes when the netlist does, so use the packer to pin a macro whose neighbour's pin plan depends on its location.
- `macro-placement` is part of a hardened block's configuration, so switching placer makes the block's abstract stale.

## Floorplan controls

Two optional `floorplan` keys steer where macros and standard cells may go.

```yaml
    floorplan:
      utilization: 0.45
      macro-anchor: upper-right        # default lower-left
      blockages:
        - rect: [10, 10, 130, 60]      # microns, die coordinates: x0 y0 x1 y1
        - rect: [400, 300, 520, 420]
          type: partial
          max-density: 0.4
```

**Macro anchor.** `macro-anchor` is the core corner the [macro packer](#macro-placement) starts from: `lower-left` (default), `lower-right`, `upper-left` or `upper-right`. Rows fill away from that corner, so the opposite edges stay clear of macros where possible. Point `pin-constraints` at those edges when a neighbouring block abuts one side. Set it per run, not per platform.

**Placement blockages.** Each `blockages` entry is `rect: [x0, y0, x1, y1]` in die microns plus a `type`:

| `type` | Effect |
| --- | --- |
| `hard` (default) | No standard cell is placed inside. The packer moves overlapping macros past it. |
| `soft` | Global placement keeps cells out; later repair and legalization may use the area. Macros may sit on it, except under `rtl-mp`. |
| `partial` | Global placement caps cell density at `max-density` (strictly between 0 and 1); legalization then treats the area as fully blocked. Macros may sit on it, except under `rtl-mp`. |

Blockages need OpenROAD 26Q1 or newer; on an older build the run fails at setup. A malformed rectangle (`x0 >= x1`, `y0 >= y1`, a side under 0.001 µm, a negative coordinate, or not four numbers), an unknown type, or `max-density` on anything but `partial` fails when `pnr.yaml` loads.

## Constrain boundary pins

Set `pin-constraints: pins.tcl` on a run to assign pins to boundary edges or groups. The path is relative to `pnr.yaml` and a missing file fails the run. Without the key, pin placement is unconstrained.

```tcl
set_io_pin_constraint -pin_names {req_* clk} -region left:*
set_io_pin_constraint -pin_names {rsp_*} -region right:*
```

- Put these commands here, not in SDC. The file is sourced just before pin placement, after macro placement and the power grid.
- Keep clock pin layers compatible with the platform's clock-routing range.
- To keep a pin edge clear of macros, anchor the packer at the opposite corner with [`floorplan.macro-anchor`](#floorplan-controls).

## PDK setup notes

The same flow runs on any PDK the root config declares. Switching PDKs is a `platform:` change in the run YAMLs.

- **Nangate45 (FreePDK45).** All defaults are calibrated on it. Set `site`, one Liberty corner, `tech-lef`, `macro-lef`, the tie and fill cells and `cts-buffer: BUF_X4`, and leave every process key unset. It has no PDN snippet, so runs have no power grid. For extraction, set `rcx-rules` to ORFS' `flow/platforms/nangate45/rcx_patterns.rules`; its LEF has no via resistance, so vias extract as 0 Ω.
- **sky130hd.** The template's `sky130hd` PDK entry is a worked example, used by the `demo_tiny_alu_subsys_hier` runs. Beyond Nangate45's fields it needs `pin-layers` (`met3` / `met2`), `routing-layers` in `met*` names, a `pdn-config`, `placement.density: 0.60`, a `dont-use-cells` list for the probe and `lpflow` cells, and a `cts-buffer` list. Use ORFS' `flow/platforms/sky130hs/rcx_patterns.rules` for `rcx-rules`.
- **ASAP7.** Follow ORFS' `flow/platforms/asap7`. Set the four [platform Tcl hooks](#source-platform-tcl-hooks) to its `liberty_suppressions.tcl`, `setRC.tcl`, `openRoad/make_tracks.tcl` and a copy of `openRoad/tapcell.tcl` with `TAP_CELL_NAME` and `MACRO_ROWS_HALO_X` / `_Y` filled in; without `tracks-tcl` the run stops at `make_tracks` with `IFP-0039`. List each corner's split Liberty files under that corner. `macro-lef` takes one path, so list the extra LEFs in `lef-paths`.

## OpenROAD threads

OpenROAD runs on one thread unless the run asks for more. Reserving CPUs from a scheduler does not change that. Set `threads:` on the run:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    threads: 8        # or: auto
```

- Unset gives 1 thread. A positive integer gives that many. `auto` gives the CPUs of the allocation the run is in, or 1 with none; never the host's core count.
- Zero, negative numbers, booleans, quoted numbers and other strings fail configuration loading.

`rb pnr` is not dispatched. Run it inside an allocation (`srun -c 8 rb pnr ...` or an `sbatch` script) to give OpenROAD CPUs. The allocation is `SLURM_CPUS_PER_TASK` (else `SLURM_CPUS_ON_NODE`) in a Slurm job, or a smaller CPU affinity mask on Linux. A container CPU quota (`docker --cpus`) is not visible.

A count above the allocation is clamped to it with an `openroad.threads_capped` warning naming both numbers. Outside an allocation an explicit count is used as given.

`--machine` output carries `openroad_threads` with the `requested`, `effective` and allocation values. The same rules apply to [`rb power`](power.md) and the OpenROAD stage of [`rb synth`](synthesis.md). Results are not guaranteed to be bit-identical across thread counts; compare DRC and slack if it matters.

## Keep stage checkpoints

A routing run killed at a scheduler wall limit leaves only its log, because the DEF and ODB are written after detailed routing. Set `checkpoints:` on the run to keep a database at each stage boundary:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    checkpoints: true            # or a stage, or a list: [cts, global_route]
```

| Stage | Written | Holds |
| --- | --- | --- |
| `floorplan` | before `global_placement` | floorplan, pins, tie cells, placed macros, PDN |
| `place` | before `clock_tree_synthesis` | legalized global placement |
| `cts` | before `global_route` | clock tree, hold repair, legalization |
| `global_route` | after a successful `global_route` | the global route |

`true` keeps all four stages; a name or list keeps some; `[]` keeps only the progress file. Unset or `false` disables the feature. Each stage writes `<NN>_<stage>.odb`, `.def` and `.sdc` into `artefacts/<run>/checkpoints/<timestamp>-<pid>/`, and `checkpoints/latest` points at the newest run.

- `tail -f artefacts/<run>/checkpoints/latest/progress.jsonl` follows a running flow. After a kill, the last `step_begin` with no `step_end` is the step the run was in.
- A checkpoint is never final. `rb power` and a plain `rb pnr-export` never read it.
- Old run directories are not pruned. Delete the ones you no longer need.

Resume from a checkpoint is not supported. To look at one, open its ODB in OpenROAD or stream it out with [`rb pnr-export --checkpoint`](#export-a-saved-result).

## Stream-out inputs

- **Layout.** Cell layouts come from the PDK's `cell-gds` (one path or a list), then the run's `gds-paths`. Put a hard macro's layout in `gds-paths`; an OpenRAM SRAM has its LEF in `lef-paths` and its GDS in `gds-paths`. Paths resolve against the file that names them.
- **LEFs.** The DEF reader gets the technology LEF, the PDK's macro LEF, then the run's `lef-paths`.
- **Record.** `def2stream.inputs.json` in the artefact directory shows what a run streamed.

A configured input that is missing on disk stops the export before KLayout starts, with every missing path reported at once.

## Stream-out completeness

A cell that the DEF instantiates but no GDS contains is streamed as an empty placeholder. Sometimes that is intended, such as an ORFS `fakeram45` macro that exists only in LEF. Sometimes a real SRAM GDS was forgotten. `gds-mode` says which the run means:

```yaml
runs:
  - name: demo_pnr_signoff
    # ...
    gds-mode: strict          # default: preview
    gds-allow-empty:          # cells that are empty on purpose
      - fakeram45_*
```

- **`preview`** (default) keeps the layout and reports what is missing: a `pnr.gds_incomplete` warning naming every cell, `GDS incomplete: …` in the run description, and `gds+png (incomplete: N missing)` in the summary. `--machine` output has `gds_status: incomplete` and `gds_missing_cells`. The run still passes.
- **`strict`** refuses to publish an incomplete layout. Cells with no layout, a missing KLayout, a PDK with no `klayout-tech`, a missing input, any stream-out failure and a failed `--png` render each make the export a `FAIL` with `fail_stage: export`. The GDS, PNG and stream-out report are removed; the routed outputs stay. `xfail:` does not excuse an export failure.

`gds-allow-empty` takes cell names or case-sensitive `fnmatch` globs. A covered cell is not missing in either mode and is counted as `(N empty by design)` in the summary.

## Export a saved result

KLayout, a PDK's GDS or the layer properties often arrive after P&R does. `rb pnr-export` streams an existing routed result out again, DEF to GDS to PNG, without rerunning synthesis or OpenROAD:

```bash
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png --gds-mode strict
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --checkpoint cts --png
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png-only --lyp dark.lyp --png-width 4096 --png-height 4096
```

Run selection, `-c`, `-l` and `--gds-mode` work as for `rb pnr`, and the same stream-out inputs and `gds-allow-empty` apply. The export needs the `synth.yaml` entry (for the design name) but none of its artefacts. `rb tool-check --required-for pnr-export` checks for KLayout only.

- **`--def <path>`** exports a DEF from elsewhere, with platform and top taken from the run. It needs a single named run.
- **`--checkpoint`** exports a [stage checkpoint](#keep-stage-checkpoints) instead of the routed result. It needs a single named run and excludes `--def`. Give a stage (`cts` or `03_cts`) for the `latest` run, `<run-id>/<stage>` for an older one, or a path to a checkpoint file. Outputs go to `checkpoints/<run-id>/export/<NN>_<stage>/` and are marked not final.
- **`--png-only`** re-renders the PNG from the GDS already in the artefact directory, without stream-out. `--lyp`, `--png-width` and `--png-height` change how it is drawn.

Before launching, the export checks that the routed DEF exists, is non-empty and declares the design the synth entry names; a mismatch fails with both names. A missing layer properties file, `klayout-tech`, input or KLayout stops it the same way. It clears only its own outputs (GDS, PNG, `def2stream.*`, `export.provenance.json`) and never touches the routed results, even when it fails.

The export is the whole verdict here. Everything requested on disk is `PASS` (exit 0), including a `preview` layout with the `GDS incomplete: …` qualifier. Anything else, in either mode, is `FAIL` with `fail_stage: export` and exit 1. `export.provenance.json` records the tool, the input DEF or GDS with its SHA-256, the render options and the outcome.

## Harden a block

A block that a larger design instances as a hard macro needs three views of its routed result: an abstract LEF, a Liberty timing model and its layout. Set `harden: true` on the block's run to publish them to `artefacts/<run>/abstract/` as `<top>.lef`, `<top>.lib`, `<top>.gds` and `abstract.manifest.json`:

```yaml
runs:
  - name: alu_block_pnr
    # ...
    platform: sky130hd_tt_block
    harden: true
```

- `harden` implies `--gds` and forces `gds-mode: strict`, so it needs KLayout.
- The run also elaborates the block's top in Yosys, with the frontend, sources, defines and Liberty of the block's synthesis, and records its parameter values as `<top>.params.json`. Parents' overrides are checked against it (see [Instance a parameterised block](#instance-a-parameterised-block)). The probe's script, log and output stay in `param_probe/` of the artefact directory until the next run. If Yosys cannot elaborate the top, the abstract is published without the record and the run warns `pnr.block_params_unrecorded`.
- The views are published together or not at all. If one cannot be produced, the run fails with `fail_stage: abstract` and leaves no `abstract/`; the routed DEF and ODB stay. Each rerun removes the previous abstract first.
- An abstract carries one corner's timing model, so `harden` on a multi-corner platform is refused before OpenROAD starts.
- The model has timing arcs only, so a parent's `rb power` sees the block as drawing 0 W. Run `rb power` on the block itself.

## Block power-grid convention

A parent ties a hardened block into its grid the way it ties in any macro: its top-level straps cross the block and drop vias onto the block's power pins. This works only if the block leaves the top layers free. rtl_buddy does not enforce the split, so give the block its own platform and PDK entry:

- The block owns the lower layers and the parent the top ones. On sky130hd the block routes and straps on met1 to met4 and exposes power pins on met4, and the parent's met5 straps run over it. A block strap on met5 blocks met5 over the whole block, so the parent's straps cannot cross it. A block that needs met5 is handled by [joining its pins](#blocks-with-supply-pins-on-the-top-strap-layer).
- Point a copy of the PDK entry at a block-level `pdn-config` whose straps stop below the parent's layers, and set the block platform's `routing-layers` to the same range.

The template's `pnr/sky130hd/pdn_block.tcl` and `sky130hd_tt_block` platform show this split.

`rb pnr` ties each block instance's power and ground pins, as the abstract LEF declares them (`USE POWER` / `USE GROUND`), when the parent's `pdn-config` leaves them on no net. A pin goes to the parent supply net of the same name, else to the parent's only power or ground net; with several candidates the run fails and asks for an `add_global_connection`. The log shows `rb: tied <inst>/<pin> to <net>`.

## Blocks with supply pins on the top strap layer

Some blocks cannot leave the parent's top layer free. A block with an OpenRAM sky130 SRAM is one: the SRAM obstructs met1 to met4 and its supply ring reaches a grid only through met5, so the block builds its own met5 straps and exposes VDD and VSS on met5. The parent's `pdngen` treats those pins as an obstruction and stops its met5 straps short of the block on both sides, so nothing reaches them.

`rb pnr` joins such pins after `pdngen`. For each block supply pin on the parent's top strap layer it finds the parent strap of the pin's net on the same track, on each side of the block, and adds one strap-shaped box along the pin that overlaps each strap end by a strap width. The log shows `rb: joined N block supply pin(s) on met5 to the parent's straps (M shape(s))`.

- **Build the block on the parent's pitch.** In the block's `pdn-config`, expose its grid on the top layer (`define_pdn_grid ... -pins {met5}`) and give its straps there the parent's width, pitch and net order, so that each pin lands on a parent strap of its own net. The block's position and orientation decide the rest: a pin is on track only if the block's origin is a whole number of pitches from the parent's strap offset, and a mirrored copy swaps which net each track carries.
- **Keep the block out of the parent's macro grids.** A macro grid connects met4 to met5 over a macro, and this block blocks both, so `pdngen` warns `PDN-0232` for its grid and fails with `PDN-0233`. Define the parent's macro grids with `-cells` or `-instances` for its other macros instead of `-default`.
- **Unjoinable pins fail the run.** A supply pin with a shape on or above the parent's top strap layer, none of whose shapes can be joined, fails with `block power: <inst>/<pin> ...`. That covers a pin off the straps of its own net (`is not on a parent <net> strap`, naming the net whose track it is on and the nearest strap of its own net), a join that would touch another net's strap, as for a rotated block (`cannot be joined`), and a pin above the parent's top strap layer. Change the block's core margin or size, or the parent's floorplan, until the pins line up. A single stray shape of a pin whose other shapes are joined, such as a short strap `pdngen` adds at a macro edge, is reported as a warning.
- The block's other pins, on layers below the top one, are connected by `pdngen` as usual.

## Assemble hardened blocks

A top-level run instances hardened blocks by naming them under `blocks:`, in `pnr.yaml` and in the `synth.yaml` entry the run reads:

```yaml
# pnr.yaml
runs:
  - name: top_pnr
    # ...
    blocks:
      - name: alu_block          # module name as instanced in the top netlist
        pnr: alu_block_pnr       # a harden: true run
        pnr-path: ../alu/pnr.yaml  # default: this pnr.yaml

# synth.yaml
syntheses:
  - name: top_synth
    # ...
    blocks:
      - name: alu_block
        pnr: alu_block_pnr
        pnr-path: ../../pnr/alu/pnr.yaml   # required here
```

P&R takes each block's LEF, Liberty and GDS from its abstract; synthesis takes the Liberty and LEF. 

- **Build the blocks first.** A run never starts a block's run. With no abstract, the consuming run fails before its tool starts, naming the block and the `rb pnr` command that builds it. The top's synthesis also needs the block hardened. `rb pnr` with no run name orders this; see [Run a whole hierarchy](#run-a-whole-hierarchy).
- **Same technology and corner.** The block's technology LEF and corner Liberty must match the consuming run's, or the run fails with a platform/corner mismatch. A multi-corner platform cannot consume single-corner abstracts.
- **Stale abstracts are refused.** If an input of the block changed since it was hardened (RTL, netlist, SDC, Liberty, LEF, snippets, its `pnr.yaml` or platform), the consuming run fails, naming the block, what changed and the command that re-hardens it. `--accept-stale` on `rb pnr` or `rb synth` consumes it anyway and the result description says `stale block abstract(s) accepted: <names>`.
- **Blackbox the module.** The top's filelist must leave the block's module a blackbox, typically a port-only stub.
- **Parameterised blocks.** A hardened block has one parameter set, the `params:` of its synthesis. A parent that overrides its parameters needs a stub that declares them; see [Instance a parameterised block](#instance-a-parameterised-block).

## Instance a parameterised block

A parent instances a parameterised block with an override such as `blk #(.W(16)) u_blk (...)`. Its stub declares the block's parameters, so the frontend accepts the override and sizes the ports:

```systemverilog
// blk_bb.sv, on the parent's filelist. The block was synthesised with params: {W: 16}.
(* blackbox *)
module blk #(parameter int W = 8, localparam int AW = $clog2(W)) (
  input logic clk, input logic [W-1:0] d, output logic [W-1:0] q);
endmodule
```

Yosys then writes the instance in a form OpenROAD cannot use:

- With `frontend: slang` it writes `blk #(.AW(32'd4), .W(32'd16)) u_blk (...)`. That is every parameter, localparams included, and OpenROAD rejects the syntax (`STA-0171`).
- With the native `verilog` frontend it derives a copy of the blackbox, `\$paramod\blk\W=s32'0...10000 u_blk (...)`, and OpenROAD has no master of that name. With many overrides Yosys names the copy by a hash, `\$paramod$<sha1>\blk`. The values cannot be read back from that name, so such an instance fails the run with a request to use `frontend: slang`.

rtl_buddy rewrites each instance as `blk u_blk (...)` before OpenROAD reads the netlist:

- `rb synth` with `blocks:` rewrites `synth_netlist.v`. Yosys does not read the block's abstract Liberty when the filelist has a parameterised `(* blackbox *)` stub of the module, because the Liberty would replace the stub and lose its parameters.
- `rb pnr` rewrites any instances that remain, for example in a netlist from a synthesis without `blocks:`, and reads the result as `pnr_netlist.v` in its artefact directory.
- `rb power` takes its blocks from the synthesis it reads, so it checks and rewrites its own `power_netlist.v` copy only when that synthesis has `blocks:`.

Each of these steps checks every instance of a block before rewriting it, so the rewrite does not hide a parent that disagrees with the hardened block. A failed check stops the run before OpenROAD reads the netlist, and its message names the instance, its line, and both values.

- **Ports match the abstract.** Every port the instance connects must be a pin or bus of the block's abstract LEF, with the same width (`port 'd' is connected to 16 bit(s), but the hardened block's pin is 8 bit(s) wide`). Every signal pin of the abstract must be connected.
  - An unconnected input or inout fails; an unconnected output is a warning. Supply pins are not checked here.
  - Widths come from the netlist's declarations, selects, concatenations and sized constants.
  - A connection rb cannot size, a positional port list or an instance array fails with the connection named.
  - This is the strongest check. Any parameter that changes a port's width is caught here, recorded or not.
- **Overrides match the elaborated block, as far as the record goes.** Every `harden: true` run elaborates the block's top once more in Yosys (scratch files in `param_probe/` of the run's artefact directory) and records its parameter values as `<top>.params.json` in the abstract. Each override on an instance, named or positional, is compared with its recorded value as a whole number; two literals of the same width compare by their bits. All instances of a block must also agree. What the record covers depends on the block's frontend:
  - **`frontend: slang`**: the record lists the top's parameters and localparams, so an override the block does not have fails. A real-valued parameter cannot be recorded; the abstract is then published without a record.
  - **native `verilog` frontend**: the record lists parameters but not localparams, so an override the record lacks is only a warning.
  - **No record**: an abstract hardened by an older rtl_buddy, or one whose top Yosys could not elaborate (warning `pnr.block_params_unrecorded`). Only the synthesis's `params:` are compared, with a warning to re-harden.
  - Warnings are reported once per block and parameter, with a count of the instances.
- **The rewrite removes nothing else.** Outside the rewritten statements the output is byte-identical to the input. Each rewritten statement, read back from the output, has the input's tokens minus the `#(...)` list and with the master renamed, and needs no further rewrite. Anything else is an internal error, not a silent edit.

The record is an abstract view, so editing or losing it makes the abstract stale. Re-harden a block (`rb pnr <block run>`) to give it a record.

## Run a whole hierarchy

`rb pnr` with no run name runs every entry in the `pnr.yaml`, each block's run before the runs that consume it. Without `blocks:`, runs go in file order.

- A block whose `pnr-path` is another `pnr.yaml` is pulled in ahead of its consumer and writes to its own suite's `artefacts/`.
- A run whose block failed is not attempted. It reports `FAIL` with `fail_stage: blocked` and a `blocked_by` list, even under `xfail`, and blocking propagates up the hierarchy. Independent runs still run.
- A block skipped by `-l` does not block its consumer, which uses the abstract already published.
- A cycle in `blocks:` (`pnr blocks: cycle: a -> b -> a`), a block naming a run its `pnr.yaml` does not define, a missing `pnr-path`, or two runs that write the same `artefacts/<run>` directory stop the command before anything runs.

## Build a hierarchy from a clean tree

On a clean tree the top's synthesis needs the blocks' abstracts. `--synth` runs each run's upstream synthesis just before it:

```sh
rb pnr -c pnr/top/pnr.yaml --synth
```

- A synthesis runs once however many runs read it, regardless of its own `reglvl`. All syntheses are resolved before the first run starts, so a misspelt `synth:` stops the command up front.
- A synthesis that does not pass fails its P&R run with `fail_stage: synth`, which blocks the run's consumers. `--accept-stale` applies to syntheses too.
- `-j N` runs up to `N` P&R runs at once (default 1). A run starts as soon as every block it names has finished. Each run is a whole OpenROAD session with its own [`threads:`](#openroad-threads), so size the two together.

## Troubleshooting

- **`pdn-config not found` or a missing `rcx-rules` file.** Fix the path in the PDK entry. The run stops at `setup`.
- **Blockages rejected at setup, or an older-OpenROAD warning.** Upgrade OpenROAD (25Q1 minimum, 26Q1 for blockages).
- **`[ERROR PDN-0179] Unable to repair all channels`.** Raise `placement.macro-halo`.
- **`N macros do not fit ...`.** Lower utilization, change the aspect ratio or lower `macro-halo`.
- **`RSZ-0090` at a slow corner.** A single-Liberty macro limits that corner. Give it a Liberty per corner.
- **`RB-DONT-USE-VIOLATION`.** The netlist instantiates an excluded cell. Change the RTL, synthesis or pattern.
- **`STA-0122 cell '<pattern>' not found`.** A `dont-use-cells` pattern matches nothing.
- **`openroad.threads_capped`.** Raise the reservation, lower `threads:` or use `auto`.
- **`GDS incomplete` or `pnr.gds_incomplete`.** Add the cells' GDS to `gds-paths`, or list intentional gaps in `gds-allow-empty`.
- **`fail_stage: export`.** KLayout, `klayout-tech`, an input file or a render failed. Fix it and run `rb pnr-export`.
- **`fail_stage: abstract`, or `harden:` refused.** A view could not be produced (read `pnr.log`), or the platform has several corners.
- **A missing or stale abstract, or a platform/corner mismatch.** Run the `rb pnr` command the message names, run `rb pnr` with no run name, or pass `--accept-stale`.
- **`fail_stage: blocked`.** A block the run consumes failed. Fix it first.
- **`block power: ... is not on a parent <net> strap`, `no single parent power net`, or `PDN-0233` on a block's macro grid.** A block's supply pins are off the parent's straps or cannot be tied. See [Blocks with supply pins on the top strap layer](#blocks-with-supply-pins-on-the-top-strap-layer).
- **`instance '<inst>' ... of block '<blk>' sets <P>=...`, `port '<p>' is connected to N bit(s)`, `instances of block '<blk>' have different parameters`, or `... named by a hash`.** The parent instances a block with parameters or ports it was not hardened with. See [Instance a parameterised block](#instance-a-parameterised-block).
- **`fail_stage: error`.** The run crashed; the exception is in the row description. Its consumers are blocked and other runs still report.
