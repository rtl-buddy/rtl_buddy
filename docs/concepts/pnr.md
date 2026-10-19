---
description: Run OpenROAD place-and-route from a mapped synthesis result, configure a physical platform, and inspect timing, DRC, GDS, and layout artefacts.
---

# Place-and-Route

`rb pnr` takes a technology-mapped `rb synth` result, runs OpenROAD placement, clock-tree synthesis and routing, and reports area, timing and DRCs. It can also stream the layout to GDS and PNG with KLayout, harden a block for reuse in a larger design, keep stage checkpoints, and run each stage as its own OpenROAD session that a later run can resume.

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

`--png` and `--gds-mode` imply `--gds`. `--stop-after`, `--resume` and `--from` end a run early or resume a staged one; see [Run stages as separate sessions](#run-stages-as-separate-sessions). KLayout runs after a successful OpenROAD run. In the default `preview` mode a KLayout failure logs a warning and leaves the P&R verdict unchanged. In `strict` mode an undelivered export fails the run; see [Stream-out completeness](#stream-out-completeness).

## Flow steps

The generated `pnr.tcl` runs these steps in order:

1. Source `platform-tcl` when set, read Liberty, LEF, the synthesis netlist and the SDC, set the design's max fanout when `max-fanout` is set, then source `layer-rc-tcl` when set.
2. Initialize the floorplan, create routing tracks and run `insert_tiecells` for the PDK's `tie-hi` and `tie-lo` ports, one tie cell per constant net.
3. Place macros, insert tap and endcap cells when `tapcell-tcl` is set, build the power grid and place the IO pins, checking [`pins-complete`](#check-that-every-port-is-placed) when set, then run `buffer_ports -inputs -outputs` when the run buffers its ports (`buffer-ports`, on by default for `harden: true`; see [Harden a block](#harden-a-block)).
4. Create the [`lifetime: global-placement`](#floorplan-controls) blockages and the [rotated-macro](#rotate-a-macro) blockages, run global placement, with `-reference_hpwl` when `placement.reference-hpwl` is set and `-routability_driven` / `-routability_use_grt` when `placement.routability-driven` / `placement.routability-use-grt` are `true`, and remove those blockages again. Then run `repair_tie_fanout` for each tie port, which gives every constant-driven load its own tie cell `placement.tie-separation` microns away (default 0).
5. Run `repair_design`, legalization, clock-tree synthesis (with `-apply_ndr` when `cts-apply-ndr` is set), setup repair when `post-cts-setup-repair` is set, hold repair and a final legalization.
6. Route globally with `-verbose` and a congestion report, after `set_global_routing_layer_adjustment` when `routing-layer-adjustment` is set, repair hold on the global-route parasitics when `global-route-hold-repair` is set, route in detail at the run's `detailed-route-verbose` level, insert fill, extract parasitics when `rcx-rules` is set, then write reports and outputs.
7. After OpenROAD exits, compare the routed DEF with every macro the floorplan located and every pin it placed exactly; see [Check the routed floorplan](#check-the-routed-floorplan).

A tie port the PDK leaves unset gets neither tie step.

The run's [`hooks:`](#run-tcl-hooks) are sourced between these steps, each at the boundary its name gives; `macro-placement` replaces the macro placement of step 3.

## Interpret results

The summary reports cell count, design area, setup and hold WNS, and the number of non-empty DRC report lines. Positive slack meets timing and zero DRC lines indicate a clean route. `wns_setup_ps`, `wns_hold_ps`, `tns_ps` (setup) and `tns_hold_ps` are converted to picoseconds from the Liberty `time_unit`; see [Synthesis: Configure tools and the PDK](synthesis.md#configure-tools-and-the-pdk).

Three result fields count cells; compare experiments on `routed_cell_count`:

- `cell_count`, the summary's Cells column, is the input netlist's instance count at the floorplan. It leaves out every cell the flow adds.
- `routed_cell_count` is the finished design's instances, including tie, repair, clock-tree and hold buffers, excluding physical-only cells.
- `physical_cell_count` is the physical-only cells: masters of LEF class `CORE SPACER`, `CORE WELLTAP` or `ENDCAP*`, or matching a PDK `fill-cells` pattern. A decap counts only when its class is `CORE SPACER` or it is listed in `fill-cells`; sky130's `decap_*` cells are class `CORE`, so they count as routed unless listed.

The flow prints the last two after `>>> Final reports` as `RB-CELL-COUNT: routed <n> physical <m>`. If counting fails, both fields are absent and the run still passes.

A pass is qualified with `pnr.no_wire_rc` when the pre-route steps had no wire RC; see [Set wire RC](#set-wire-rc).

Every run that reached the end of the flow carries `routed: true`. A run ended by [`--stop-after`](#stop-after-a-stage) carries `routed: false` and `stopped_after`, and its numbers come from a stage report, not from a routed design.

`max_slew_violation_count`, `max_capacitance_violation_count` and `max_fanout_violation_count` count the routed design's pins that break a Liberty or SDC max-slew, max-capacitance or max-fanout limit, over every corner, as ORFS's `report_metrics` counts them. The summary shows them as the Slew/Cap/Fanout column, `pnr.log` prints them as `RB-ELECTRICAL: max_<check> <n>` after `>>> Final reports`, and `electrical.rpt` lists the violating pins. A pass with any violator is qualified `electrical N max-slew, ... violator(s)` and logs the warning `pnr.electrical_violators`. A count OpenROAD cannot report is left out.

A run passes when OpenROAD exits 0 with no `[ERROR ...]` line, the routed DEF keeps every located macro and exact pin where the floorplan put it, and, under `gds-mode: strict`, the requested export was delivered complete. With `fail-on-electrical: true` a run with any electrical violator also fails, with no `fail_stage`, so `xfail:` can excuse it; it keeps its routed outputs and publishes no abstract. It skips when `reglvl` filters it out or `tool:` is unsupported. Timing violations and DRC counts are metrics only; gate signoff on them in your project.

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
| `electrical.rpt` | Max-slew, max-capacitance and max-fanout violators across all corners |
| `route.drc.rpt`, `route.maze.log` | DRC summary and detailed-route log |
| `congestion.rpt` | Overflowing global-route tiles; written only when global routing overflows |
| `<top>.gds`, `<top>.png`, `klayout.*.log` | Optional KLayout outputs and logs |
| `export.provenance.json` | What the last `rb pnr-export` read and produced |
| `checkpoints/`, `abstract/` | Optional [stage checkpoints](#keep-stage-checkpoints), with a [staged run's](#run-stages-as-separate-sessions) stage scripts and logs, and [hardened-block abstract](#harden-a-block) |

Each run deletes the previous run's outputs first, and a run that fails after writing the routed database removes it again, so `rb power` never reads a stale one. `pnr.log`, `pnr.tcl` and `congestion.rpt` are kept from a failed run.

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
- **`placement.rotated-macro-footprint`** (`block`, `diamond-legalizer` or `none`; default `block`) works around OpenROAD's legalizer for macros turned by 90 degrees; see [Rotate a macro](#rotate-a-macro).
- **`placement.routability-driven`** (default `false`) adds `-routability_driven` to `global_placement`: the placer estimates congestion, inflates cells in congested regions and re-spreads them. Turn it on for a congested block whose global route overflows. **`placement.routability-use-grt`** (default `false`) adds `-routability_use_grt`, which estimates that congestion with the global router instead of RUDY; slower, closer to the real route. It takes effect only in routability mode, so a platform whose resolved settings enable it without `routability-driven` fails to load. Like the other `placement.*` keys, each is set on the PDK and overridden per platform.
- **`cts-buffer`** takes a name or a list. With a list, the first entry is the root buffer.
- A run may set its own `placement:` block in `pnr.yaml` with any of these keys. It overrides the platform, which overrides the PDK, field by field, and the `routability-use-grt` rule applies to the resolved values:

  ```yaml
  runs:
    - name: blk_pnr
      # ...
      placement:
        density: 0.25
        rotated-macro-footprint: diamond-legalizer
  ```
- **`port-buffer`** (unset by default) is the cell `buffer_ports` inserts on each port of a run with `buffer-ports` on, passed as `-buffer_cell`; unset lets OpenROAD choose. See [Harden a block](#harden-a-block).
- **`cts-sink-clustering`** (default `true`) passes `-sink_clustering_enable` to CTS. Set it to `false` when CTS fails with `CTS-0080 Sink not found`, as it can on coincident clock pins or a large ASAP7 clock tree.
- **`cts-apply-ndr`** (unset by default) passes `-apply_ndr` (`none`, `root_only`, `half` or `full`) to CTS; unset keeps OpenROAD's default, `half`. Set `none` when the global router detours clock nets that carry non-default rules to several times their Manhattan length, which shows as large hold violations and `GRT-0273 Disabled NDR` in the log.
- **`max-fanout`** (positive integer, unset by default) adds `set_max_fanout <n> [current_design]` after each `read_sdc` in `rb pnr` and `rb power`, replacing a design-level limit from the SDC. When neither the Liberty nor the SDC sets a limit, `repair_design` uses 50 and buffers every larger net; on a library without one, such as SKY130 HD, that costs setup timing. A large value such as `100000` stops repair buffering for fanout alone.
- **`post-cts-setup-repair`** (default `false`) runs `repair_timing -setup` after CTS, before hold repair. Turn it on when the post-CTS netlist misses setup; it adds buffers and resizes cells, so QoR changes.
- **`global-route-hold-repair`** (default `false`) repairs hold again after global route, on `estimate_parasitics -global_routing`, then legalizes and reroutes the new buffers before detail route, as ORFS does; see [Set wire RC](#set-wire-rc).
- **`routing-layer-adjustment`** (0 to 1, unset by default) withholds that fraction of each signal layer's capacity from the global router (`set_global_routing_layer_adjustment`, ORFS `ROUTING_LAYER_ADJUSTMENT`, 0.25 on ASAP7). Raise it when detailed routing ends with DRCs in congested areas; unset leaves the router's default.
- **`pdn-config`** is a path, resolved from `root_config.yaml`, to a Tcl snippet that declares the power grid (`add_global_connection`, `set_voltage_domain`, `define_pdn_grid`, `add_pdn_stripe`, `add_pdn_connect`). The flow sources it after macro placement and calls `pdngen` itself, so the snippet must not. Unset means no power grid. A run can replace it or its core grid; see [Plan the power grid per run](#plan-the-power-grid-per-run).
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

## Run Tcl hooks

A block that needs a step the generated flow does not have, such as its own macro placement, a density screen, an extra repair or a report, names Tcl files under `hooks:` on its run, and `rb pnr` sources them at fixed points of the flow. No edit of the generated `pnr.tcl` is needed. Each point takes one path or a list, relative to `pnr.yaml`:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    hooks:
      session: [wire_rc.tcl]
      macro-placement: [macros.tcl]
      after-cts: [io_latency.tcl, clock_report.tcl]
```

| Point | Sourced |
| --- | --- |
| `session` | After `read_sdc`, `max-fanout`, `layer-rc-tcl` and the don't-use cells, before the floorplan |
| `after-floorplan` | After the floorplan, tracks, blockages, cut-outs and tie cells, before macro placement |
| `macro-placement` | In place of rb's macro placement (the packer or RTL-MP), inside the macro branch |
| `after-pdn` | After the tap cells and the power grid, before IO pin placement |
| `after-pins` | Right after `place_pins`, before `buffer_ports` |
| `after-global-placement` | Right after `global_placement`, before `repair_tie_fanout` |
| `after-repair-design` | Right after `repair_design`, before the legalizing `detailed_placement` |
| `after-cts` | Right after `clock_tree_synthesis`, before the post-CTS legalization and the setup and hold repair, so the repair sees what the hook sets |
| `after-global-route` | Right after `global_route`, before the global-route hold repair |
| `before-detailed-route` | Immediately before `detailed_route` |
| `before-reports` | After fill insertion and parasitic extraction, before the final reports |

- The files of a point are sourced in list order at Tcl global scope, so the flow's variables are visible to them: `$OUT_DIR` and `$DESIGN` throughout, `$block` and `$macros` from macro placement on. A variable one file sets is still set in the next. `pnr.log` announces each file as `>>> Hook <point>: <path>`.
- OpenROAD runs in the run's artefact directory, `artefacts/<run>/`, so a hook reaches files next to itself through `[file dirname [info script]]`.
- `session` is for state the saved database does not keep, such as `set_wire_rc` on a run's own layers or extra `set_dont_use` cells. The PDK's `layer-rc-tcl` stays the wire RC rb knows about: the [no-wire-RC qualifier](#set-wire-rc) and a [`netlist-source: pnr` power run](power.md#extracted-parasitics) read only that. The post-route don't-use check covers only the configured cells, not ones a hook adds.
- `macro-placement` runs inside the flow's macro branch, so it does not run when the netlist has no macros. The [`floorplan.macros`](#place-individual-macros) directives still apply before it, and the cell-halo blockages around every macro are still created after it. `$rb_movable_macros` lists the macros the directives leave unlocated, or every macro when there are no directives. Setting `floorplan.macro-placement` or `floorplan.macro-anchor` together with the hook is a config error, since neither would have an effect.
- Results come from the final reports: a report a hook prints earlier does not change the result's area, slack or TNS.
- `after-pdn` runs whether or not the run has a pdn-config.
- `pin-constraints` is unchanged: it is sourced just before `place_pins`, and `after-pins` runs after it.
- A hook that raises a Tcl error fails the run, and the error names the point and file: `Hook after-cts (<path>): <message>`. OpenROAD stops there. An OpenROAD `[ERROR ...]` line a hook catches still fails the run, as anywhere in the flow. `exit`, a top-level `break` or `continue`, and a `return` that reaches past the hook file are such errors too, since each would end the flow unfinished.
- A configured file missing from disk fails the run at `setup`, naming `hooks.<point>[<index>]`, before OpenROAD starts.
- Every hook file is an input to [stage checkpoints](#keep-stage-checkpoints) and to a [hardened block's](#harden-a-block) abstract, so editing one makes the abstract stale. `hooks` is part of the block's configuration only when set.
- Commands a hook runs report their progress like the flow's own, but they do not move a stage checkpoint: a `global_placement` inside a hook leaves the `floorplan` checkpoint to the flow's own `global_placement`.
- In a [staged run](#run-stages-as-separate-sessions) each hook point is sourced in the session of the stage that contains it, and `session` hooks are sourced again in every session. Session state a hook sets, such as a Tcl procedure, a `rename` wrapper or `set_wire_rc`, is gone in the next session; see [what a session keeps](#run-stages-as-separate-sessions). A `session` hook keeps it in every session, but from the first one on, so it also changes the steps before the hook point that it was written for.
- `after-pins` runs before rb's `buffer_ports`. A hook that buffers the ports itself needs `buffer-ports: false`, or rb buffers them again after it.

## Set wire RC

Set the PDK's `layer-rc-tcl` so clock-tree synthesis, placement parasitics and hold repair see wire resistance and capacitance. Without it they see only the technology LEF's layer RC, which is zero on most open PDKs. CTS then balances, and hold repair fixes, a design with no wires, and the routed timing can fail hold that repair never saw.

`rb pnr` warns with `pnr.no_wire_rc` when the PDK sets no `layer-rc-tcl`, or when `pnr.log` has OpenROAD's `[WARNING EST-0018]` (zero wire capacitance) or `[WARNING CTS-0104]` (zero clock wire RC). The warning is logged and appended to a passing run's description, naming the cause:

```text
P&R passed; pnr.no_wire_rc: pre-route steps saw no wire RC (no layer-rc-tcl, EST-0018, CTS-0104); see rb docs show concepts/pnr#set-wire-rc
```

For sky130hd, copy the `set_layer_rc` lines of OpenROAD's `test/sky130hd/sky130hd.rc` and add the wire RC layers for signal and clock nets:

```tcl
# pdk/sky130hd/setRC.tcl, named by cfg-pdks layer-rc-tcl
set_layer_rc -layer met1 -capacitance 1.72375E-04 -resistance 8.929e-04
# ... one line per layer and via, from sky130hd.rc
set_wire_rc -signal -layer met2
set_wire_rc -clock -layer met5
```

To also catch hold growth from wire lengths before detail route, set `global-route-hold-repair: true` on the platform. It runs `repair_timing -hold` on the global-route estimate, then legalizes the added buffers and reroutes their nets incrementally. It is off by default because it changes QoR. A `global_route` [checkpoint](#keep-stage-checkpoints) holds the route before this repair.

## Macro placement

A netlist that instantiates hard macros, such as an SRAM or a partition hardened by an earlier run, gets automatic macro placement before the power grid is built. To use OpenROAD's placer instead, see [RTL-MP macro placement](#rtl-mp-macro-placement).

- The packer sorts macros tallest first, then widest, and packs them left to right into rows starting at the core's lower-left corner, or at the corner [`floorplan.macro-anchor`](#floorplan-controls) names.
- It keeps `placement.macro-halo` between neighbours and at every core edge, and snaps each origin to the standard-cell site grid.
- Placed macros are `FIRM`; global and detailed placement leave them where they are.
- To fix, orient or halo a single macro, see [Place individual macros](#place-individual-macros).

When macros do not fit, the run fails with `N macros do not fit the W x H um floorplan core`, followed by the largest footprint, the macro and core areas, and the smallest core at the run's aspect ratio that would fit. Lower the floorplan utilization, change its aspect ratio, or lower `placement.macro-halo`.

## RTL-MP macro placement

`floorplan.macro-placement: rtl-mp` (default `pack`) hands macros to OpenROAD's hierarchical macro placer, `rtl_macro_placer`, instead of the packer:

```yaml
    floorplan:
      utilization: 0.45
      macro-placement: rtl-mp
```

RTL-MP places macros by connectivity and wirelength and may flip them (`MX`, `MY` or `R180`), but never [turns](#rotate-a-macro) one by 90 degrees. Macros end up `LOCKED` on the track grid, and reports go to `artefacts/<run>/rtlmp/`. Use it for a flat netlist with many memories or when timing depends on macro placement; on the template's sky130hd assembly it gave about 0.8 ns better setup slack than the packer.

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

**Macro anchor.** `macro-anchor` is the core corner the [macro packer](#macro-placement) starts from: `lower-left` (default), `lower-right`, `upper-left` or `upper-right`. Rows fill away from that corner, so the opposite edges stay clear of macros where possible. Constrain pins to those edges with [`floorplan.pins`](#constrain-boundary-pins) when a neighbouring block abuts one side. Set it per run, not per platform.

**Placement blockages.** Each `blockages` entry is `rect: [x0, y0, x1, y1]` in die microns plus a `type`:

| `type` | Effect |
| --- | --- |
| `hard` (default) | No standard cell is placed inside. The rows under it are cut before tap insertion, so no tap or endcap lands inside and the cut row ends get endcaps. The packer moves overlapping macros past it. |
| `soft` | Global placement keeps cells out; later repair and legalization may use the area. Macros may sit on it, except under `rtl-mp`. |
| `partial` | Global placement caps cell density at `max-density` (strictly between 0 and 1); legalization then treats the area as fully blocked. Macros may sit on it, except under `rtl-mp`. |

A row cut rounds outward to whole sites, so the remnants stay on the site grid. A remnant narrower than twice the row height is dropped. The power grid's followpins follow the cut rows.

**Global-placement-only blockages.** `lifetime: global-placement` on an entry of any type (default `flow`) makes it a screen for global placement alone:

```yaml
      blockages:
        - rect: [400, 300, 520, 420]
          type: partial
          max-density: 0.35
          lifetime: global-placement
```

It is created immediately before `global_placement` and removed immediately after, by the handle the flow created, so legalization, repair and routing never see it. No row is cut under it, the packer and RTL-MP do not avoid it, and no [checkpoint](#keep-stage-checkpoints) holds it.

Blockages need OpenROAD 26Q1 or newer; on an older build the run fails at setup. A malformed rectangle (`x0 >= x1`, `y0 >= y1`, a side under 0.001 µm, a negative coordinate, or not four numbers), an unknown type, or `max-density` on anything but `partial` fails when `pnr.yaml` loads.

## Place individual macros

`floorplan.macros` gives individual macros a fixed location, an orientation or their own standard-cell halo. Each entry names an `instance`, by its full hierarchical name or a glob over the names. A floorplanner's output can come from a [file](#read-macros-and-pins-from-planner-files) instead:

```yaml
    floorplan:
      macros:
        - instance: u_inner/u_hist_mem/u_sram
          location: [200, 290]         # lower-left corner, die microns
          orientation: MX
          halo: [3, 3]                 # standard-cell keep-out, x and y, microns
        - instance: "*/u_c*"           # every matching macro
          orientation: R180
```

| Key | Meaning |
| --- | --- |
| `instance` | A macro instance name as OpenROAD reads it from the netlist, such as `u_inner/u_hist_mem/u_sram`. A name that is not an instance is matched as a glob, where `*` also crosses `/`. |
| `location` | `[x, y]`, the lower-left corner of the macro's box after orientation, in die microns. The macro is fixed (`LOCKED`) before the packer or RTL-MP runs, and both leave it there. The pattern must match exactly one macro. |
| `orientation` | `R0`, `R90`, `R180`, `R270`, `MX`, `MY`, `MXR90` or `MYR90`. Applied before any location and before placement, so a packed macro keeps it. See [Rotate a macro](#rotate-a-macro). |
| `halo` | `[x, y]` in microns. Replaces `placement.macro-cell-halo` for the matched macros: no standard cell is placed within `x` of the macro's left and right edges or `y` of its top and bottom. `[0, 0]` removes the halo. |

- A location snaps to the nearest standard-cell site and row from the core's lower-left corner, the grid the packer also uses, so the macro's bottom edge lies on a row boundary. A macro that fits the core before snapping still fits after. The log shows `fixed <inst> at (x, y) um <orientation> (requested (x, y))`.
- `floorplan.macro-snap: false` (default `true`) places every located macro at its exact location, rounded once to the database unit, off the site and row grid. The location is then the floorplanner's to keep on the grids the flow relies on: a macro whose bottom edge lies between two rows can fail legalization with `DPL-0011`.
- A fixed macro must lie inside the core and must not overlap another fixed macro; either fails the run naming the entry. Overlap with a placement blockage is allowed.
- With the [packer](#macro-placement), fixed macros are keep-outs: the other macros stay `placement.macro-halo` away from them, as from each other. With `rtl-mp`, RTL-MP treats them as fixed obstacles.
- Under `rtl-mp`, an `orientation` without a `location` is refused, because RTL-MP picks each placed macro's orientation itself.
- A macro may be matched by several entries, inline or from the file, but only one entry may set each key for it.
- At run time a pattern that matches no instance, or that matches a standard cell, fails the run: `floorplan.macros[i]: no instance matches '<pattern>'`.
- A `location` on a netlist with no hard macros fails the run at macro placement, naming up to three located entries: `floorplan.macros: the netlist has no hard macros, but 1 entry gives a macro location: floorplan.macros[0] (u_sram)`.
- `floorplan.macros` is part of a hardened block's configuration. For a [hardened block](#assemble-hardened-blocks) placed with a `location`, check that its supply pins land on the parent's straps, and that a mirrored block's net order still fits the parent's grid.

## Rotate a macro

`R90`, `R270`, `MXR90` and `MYR90` turn a macro by 90 degrees, so its box is the master's height wide and its width tall. `location` is the lower-left corner of that turned box, and the packer packs a turned macro by its turned size. RTL-MP does not turn macros.

Turning a macro turns its pins and power ring with it, so pin access and power are the floorplan's to check: horizontal pin shapes become vertical, and a PDK power grid written for unrotated macros may have no grid for the turned orientations or connect layers that no longer cross. `pdngen` then fails, for example with `PDN-0233` when a macro grid gets no via. Give the PDK's `pdn-config`, or the run's own, a macro grid for the turned orientations that reaches the turned power pins.

OpenROAD's default legalizer blocks a turned fixed macro over its master's unrotated width and height from the macro's lower-left corner, not over its real box (OpenROAD#11735). Cells that global placement put in the part of that footprint outside the macro are then moved out by legalization, some far. `placement.rotated-macro-footprint` (PDK, platform or run [placement](#tune-the-process-dependent-steps) key) chooses the workaround; it acts only when a `floorplan.macros` or macros-file entry turns a macro, or when the run has a [hook](#run-tcl-hooks) before global placement (`session`, `after-floorplan`, `macro-placement`, `after-pdn` or `after-pins`), which may turn one:

| Value | Effect |
| --- | --- |
| `block` (default) | A hard placement blockage over each turned macro's unrotated footprint, grown by its [cell halo](#tune-the-process-dependent-steps), then outward to whole rows and sites as the legalizer rounds it, and clipped to the core, exists during global placement only, like a [`lifetime: global-placement`](#floorplan-controls) blockage. No cell is placed where legalization would refuse it, and the area outside the macro stays empty. |
| `diamond-legalizer` | Every `detailed_placement`, including the one after post-global-route hold repair, runs `-use_diamond_legalizer`, which sees the macro's real box. |
| `none` | Neither. |

The log names each blocked footprint under `>>> Rotated macros`, or the switch to the diamond legalizer.

## Read macros and pins from planner files

A floorplanner that writes hundreds of macro locations or thousands of pin positions can hand them over as files instead of `pnr.yaml` entries:

```yaml
    floorplan:
      macros-file: plan/macros.yaml   # floorplan.macros entries
      macro-snap: false               # exact locations, no site or row snapping
      pins-file: plan/pins.json       # exact pins and floorplan.pins entries
      pin-snap: false                 # exact pin centres, not moved to the edge or a track
      pins-complete: true             # every signal port must be covered
```

```yaml
# plan/macros.yaml
- {instance: u_core/u_sram0, location: [120.0375, 80.64], orientation: R90}
- {instance: "u_core/u_rf*", halo: [2, 2]}
```

```json
[
  {"name": "din[0]", "location": [0.5, 68.34], "layer": "met3", "size": [1.0, 0.3]},
  {"names": ["dout*"], "side": "right"}
]
```

- Each file is a YAML or JSON list, its path relative to `pnr.yaml`. It is read when the run starts, before OpenROAD, so `rb pnr --list` does not need it. A missing file, a malformed entry or an unknown key fails the run with `fail_stage: setup`, naming the file and the entry's index.
- A macros-file entry has the [`floorplan.macros`](#place-individual-macros) keys. A pins-file entry is either an exact pin, `name` (or `names` with one name) with `location`, optional `layer` and optional `size`, or any [`floorplan.pins`](#constrain-boundary-pins) entry. `size` is `[width, height]` in microns, the pin's x and y extents, as in `floorplan.pins` and `place_pin -pin_size`.
- File entries apply after the inline ones and keep their order: exact pins from `floorplan.pins`, then from the file, then the inline and file side and group rules, then `pin-constraints`. Messages name them `floorplan.macros-file[i]` and `floorplan.pins-file[i]`.
- File coordinates reach OpenROAD at full precision and are rounded once, to the database unit. Inline coordinates are rounded to the nanometre.
- Both files are fingerprinted as inputs of a [checkpoint](#keep-stage-checkpoints) run and of a [hardened block](#harden-a-block), so editing one makes the block's abstract stale.

`macro-snap: false` is described under [Place individual macros](#place-individual-macros). `pin-snap: false` (default `true`) places each exact pin, inline or from the file, with its centre at its location to the database unit: `place_pin` runs without `-force_to_die_boundary`, so the pin is neither moved onto the die edge nor onto a routing track. Give a pin on an edge a centre half its depth inside the die. Without the force, placing a pin is also much faster, which matters for thousands of pins. The log still reports each pin's requested and final centre.

### Check that every port is placed

`floorplan.pins-complete: true` (default `false`) checks, just before the `pin-constraints` file and `place_pins`, that every port other than a power or ground port is covered by the inline and file pin entries. An exact pin covers its port; a side or group rule covers the ports it matches that have no exact pin. The run fails, listing up to ten ports per problem and the count:

- a port with more than one exact entry;
- a port with no exact entry that several side or group rules match;
- a port nothing matches.

The message reads `floorplan.pins-complete: 2 port(s) matched by no entry (irq, test_mode)`, and the result carries `fail_stage: pins_complete`, so `xfail:` does not excuse it. A port constrained only by the `pin-constraints` file counts as unmatched.

### Check the routed floorplan

A run with a located macro or an exact pin compares them with the routed `<top>.def` after OpenROAD succeeds. `pnr.log` records each as placed, `RB-FLOORPLAN-MACRO: <x> <y> <orientation> <instance>` and `RB-FLOORPLAN-PIN: <x0> <y0> <x1> <y1> <layer> <port>` in database units, and the check reads only the DEF's COMPONENTS and PINS. A macro at another location or orientation, or a pin with another layer or box, fails the run with `fail_stage: routed_check`, naming the first three differences, so `xfail:` does not excuse it. The routed outputs stay for inspection and no abstract is published.

## Size the die and core

By default `utilization`, `aspect` and `core-margin` size the floorplan from the netlist's cell area. To give exact dimensions instead, set both rectangles in die microns:

```yaml
    floorplan:
      die-area: [0, 0, 160, 160]       # x0 y0 x1 y1
      core-area: [20, 20, 140, 140]
      core-cutouts:
        - [80, 80, 140, 140]           # an L-shaped core
```

- `die-area` and `core-area` go together and run `initialize_floorplan -die_area -core_area`. They exclude `utilization`, `aspect` and `core-margin`; setting any of those with them is a load error. The core must lie inside the die. OpenROAD snaps the core edges inward onto the site grid and logs `IFP-0028`.
- `core-cutouts` carves rectangles out of the core for an L, T or other rectilinear core. Each is a hard placement blockage whose rows are cut before tap insertion, like a `hard` [blockage](#floorplan-controls), and a keep-out for the macro packer. With `core-area` set, a cut-out must lie inside it; at run time, one that does not overlap the core fails the run. It works with utilization sizing too, but the core is still sized for the whole cell area, so lower `utilization` to leave room.
- The die stays rectangular: OpenROAD has no non-rectangular die. A cut-out keeps standard cells, rows, taps, followpin rails and macros out; it does not block routing. Core-grid straps still cross it and stay connected through the rows on either side, so `check_power_grid` passes. IO pins are still placed along the whole die edge; keep them off the edges next to a cut-out with [`floorplan.pins`](#constrain-boundary-pins).
- All three keys are part of a hardened block's configuration when set.

## Plan the power grid per run

The PDK's `pdn-config` declares the power grid for every run on it. A run can replace or reshape it:

- **`pdn-config:`** on the run is a Tcl file, relative to `pnr.yaml`, sourced instead of the PDK's. It follows the same contract: declare the grid, never call `pdngen`.
- **`pdn:`** on the run declares the core grid in YAML. It replaces the core grid of the pdn-config in effect (the run's or the PDK's), which must exist. Everything else in that file still runs: global connections, voltage domains and macro grids.

```yaml
    pdn:
      ring: {layers: [met5, met4], width: 1.6, spacing: 1.7, offset: 2}
      stripes:
        - {layer: met1, width: 0.48, followpins: true}
        - {layer: met4, width: 1.6, pitch: 20, offset: 5}
        - {layer: met5, width: 1.6, pitch: 20, offset: 5}
      # connect: [[met1, met4], [met4, met5]]   # default: each stripe layer to the next
```

| Key | Meaning |
| --- | --- |
| `ring` | Optional core ring: `layers` `[horizontal, vertical]`, `width`, `spacing` between the power and ground rings, and `offset` from the core edge, all in microns (`add_pdn_ring -core_offsets`). It needs `offset + 2 x width + spacing` of room between core and die, which is checked against `core-margin` or the explicit areas when the run loads. With a ring, every stripe extends to it. |
| `stripes` | The core grid's stripes, bottom layer first. A `followpins: true` entry gives the rails along the standard-cell rows and takes only `width`. Any other stripe needs `pitch`, and takes optional `offset` and `spacing` (`add_pdn_stripe`). |
| `connect` | Optional `[lower, upper]` layer pairs to join with vias (`add_pdn_connect`). By default each stripe layer is joined to the next one listed. A ring on layers that also carry stripes needs nothing more; put other ring layers in `connect`. |

While it sources the pdn-config, the flow skips each `define_pdn_grid` that is neither `-macro` nor `-existing`, and that grid's stripes, rings and connects, and logs `pdn: core grid '<name>' from the pdn-config replaced by the run's pdn: block`. The run's grid keeps the skipped grid's name, voltage domains, `-pins` layers and starting net; without one it is `rb_core` on OpenROAD's default domain. Macro grids that connect to the core straps, as the template's `-grid_over_boundary` grids do, keep working only if `pdn:` still has straps on the layers they connect to.

- `pdn:` with no pdn-config at all (for example on Nangate45) fails at setup: the global connections and voltage domain have to come from Tcl.
- Malformed entries fail at load: a stripe without `pitch`, two stripes wider than their pitch, `pitch` on a followpins entry, a ring without two layers, or a ring too wide for the core-to-die gap.
- Switchable power domains are not supported: they need power-intent (UPF) support, which `rb pnr` does not have. Use one always-on domain.
- `pdn-config` and `pdn` are part of a hardened block's configuration when set.

## Constrain boundary pins

`floorplan.pins` constrains IO pins in `pnr.yaml`. Each entry names ports by name or glob in `names`, and either constrains them to an edge, keeps them together, or places one pin exactly:

```yaml
    floorplan:
      pins:
        - names: [req_*, clk]          # the whole left edge
          side: left
        - names: rsp_*
          side: right
          start: 10                    # microns, die y from 10 to 80
          end: 80
        - names: pwdata                # a bus: every bit, together, in bit order
          side: top
          group: true
          order: true
        - names: irq
          location: [0, 42.5]          # pin centre, die microns
          layer: met3                  # optional
          size: [0.3, 0.6]             # optional, microns
```

| Key | Meaning |
| --- | --- |
| `names` | Port names or globs, one string or a list. A bus name matches every bit. |
| `side` | `left`, `right`, `top` or `bottom`: `set_io_pin_constraint -region <side>:<start>-<end>`. |
| `start` / `end` | Optional range along the side in die microns: y for `left` and `right`, x for `top` and `bottom`. Either one alone leaves the other end open. Without both the whole edge is allowed. |
| `group` / `order` | `group: true` keeps the matched pins next to each other (`-group`); `order: true` also keeps them in the order matched. With no `side` the group may land on any edge. |
| `location` | `[x, y]` pin centre in die microns. Places exactly one pin with `place_pin -force_to_die_boundary`, so it moves onto the nearest die edge for its layer's direction and onto the nearest routing track; the log reports the requested and the final centre. `floorplan.pin-snap: false` places it at its centre instead; see [Read macros and pins from planner files](#read-macros-and-pins-from-planner-files). The pin is fixed, and pin placement leaves it there. |
| `layer` | With `location` only. Default: the PDK's horizontal pin layer on a left or right edge, its vertical one on a top or bottom edge, judged by the nearest edge. |
| `size` | With `location` only. `[width, height]` in microns: the pin's x and y extents, as `place_pin -pin_size` takes them. Default: the layer's minimum width and area. |

- Exact pins are placed first, then the edge and group constraints, then the `pin-constraints` file below, all just before `place_pins`. A side or group rule leaves out the pins placed exactly, so a pin both placed and matched by a side keeps its location.
- A name or glob that matches no port fails the run with `floorplan.pins[i]: no port matches '<pattern>'`, where `set_io_pin_constraint` alone would only warn. A `location` entry whose name matches more than one port fails too.
- A malformed entry fails when `pnr.yaml` loads, naming `floorplan.pins[i]`: an unknown side, `start` at or above `end`, a negative coordinate, `start` or `end` without `side`, `order` without `group`, `location` with `side`, `group` or more than one name, `layer` or `size` without `location`, or an entry that sets none of `side`, `group` and `location`.
- A `harden: true` run places its pins the same way, and the published abstract carries them. `floorplan.pins` is part of a hardened block's configuration, so changing it makes the block's abstract stale.
- Keep clock pins on layers inside the platform's clock-routing range. To keep a pin edge clear of macros, anchor the packer at the opposite corner with [`floorplan.macro-anchor`](#floorplan-controls).

For anything `floorplan.pins` does not cover, such as `-mirrored_pins` or `exclude_io_pin_region`, set `pin-constraints: pins.tcl` on the run. The path is relative to `pnr.yaml` and a missing file fails the run. The file is sourced just before pin placement, after macro placement, the power grid and `floorplan.pins`:

```tcl
exclude_io_pin_region -region bottom:0-20
set_io_pin_constraint -mirrored_pins {a_in a_out}
```

Put these commands here, not in SDC, which is read before the die exists.

## PDK setup notes

The same flow runs on any PDK the root config declares. Switching PDKs is a `platform:` change in the run YAMLs.

- **Nangate45 (FreePDK45).** All defaults are calibrated on it. Set `site`, one Liberty corner, `tech-lef`, `macro-lef`, the tie and fill cells and `cts-buffer: BUF_X4`, and leave every process key unset. It has no PDN snippet, so runs have no power grid. For extraction, set `rcx-rules` to ORFS' `flow/platforms/nangate45/rcx_patterns.rules`; its LEF has no via resistance, so vias extract as 0 Ω.
- **sky130hd.** The template's `sky130hd` PDK entry is a worked example, used by the `demo_tiny_alu_subsys_hier` runs. Beyond Nangate45's fields it needs `pin-layers` (`met3` / `met2`), `routing-layers` in `met*` names, a `pdn-config`, `placement.density: 0.60`, a `dont-use-cells` list for the probe and `lpflow` cells, and a `cts-buffer` list. Use ORFS' `flow/platforms/sky130hs/rcx_patterns.rules` for `rcx-rules`.
- **ASAP7.** Follow ORFS' `flow/platforms/asap7`. Set the four [platform Tcl hooks](#source-platform-tcl-hooks) to its `liberty_suppressions.tcl`, `setRC.tcl`, `openRoad/make_tracks.tcl` and a copy of `openRoad/tapcell.tcl` with `TAP_CELL_NAME` and `MACRO_ROWS_HALO_X` / `_Y` filled in; without `tracks-tcl` the run stops at `make_tracks` with `IFP-0039`. List each corner's split Liberty files under that corner. `macro-lef` takes one path, so list the extra LEFs in `lef-paths`.

## Follow routing progress

`pnr.log` is OpenROAD's output, written as it happens, so `tail -f artefacts/<run>/pnr.log` follows a running flow.

- Every global route runs with `-verbose`. After it, `pnr.log` has the per-layer resource and usage table (`[INFO GRT-0096] Final congestion report:`), which shows the congestion margin of a passing run. With `global-route-hold-repair`, the incremental reroute prints the table again.
- When global routing overflows, each overflowing tile, with its capacity, usage and nets, goes to `congestion.rpt`, in the DRC-report format the OpenROAD GUI's DRC viewer loads. A route that fails with `GRT-0116` keeps it.
- Detailed routing logs each optimization iteration (`[INFO DRT-0195] Start 1st optimization iteration.`), its progress (`Completing 30% with 88 violations.`) and its result (`[INFO DRT-0199] Number of violations = 16.`), so a converging route can be told from a stalled one. Set `detailed-route-verbose:` on the run to change the level: `0` silences the iterations; higher levels add more router detail.

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    detailed-route-verbose: 0    # default 1
```

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

Without `--dispatch`, `rb pnr` runs OpenROAD where it is started. Run it inside an allocation (`srun -c 8 rb pnr ...` or an `sbatch` script) to give OpenROAD CPUs, or give each stage a job of its own with [`--dispatch slurm`](#dispatch-the-stages-to-slurm). The allocation is `SLURM_CPUS_PER_TASK` (else `SLURM_CPUS_ON_NODE`) in a Slurm job, or a smaller CPU affinity mask on Linux. A container CPU quota (`docker --cpus`) is not visible.

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
| `floorplan` | before `global_placement` | floorplan, pins, tie cells, placed macros, PDN; no global-placement-only blockage |
| `place` | before `clock_tree_synthesis` | legalized global placement |
| `cts` | before `global_route` | clock tree, hold repair, legalization |
| `global_route` | after `global_route`, the `after-global-route` hooks and the global-route hold repair, before the `before-detailed-route` hooks | the global route |
| `detailed_route` | after `detailed_route` | the detailed route, before fill |

In a one-session run the flow ends each stage, but `global_route`, on the command the table names: on entering `global_placement`, `clock_tree_synthesis` or `global_route`, or on leaving `detailed_route`. A hook that wraps one of these commands with `rename` hides that boundary, which a one-session run does not support: a run that reaches its end without writing a checkpoint it keeps fails with `fail_stage: checkpoint`, naming the stage and the command. A [staged run](#run-stages-as-separate-sessions) ends each stage at the end of its session and has no such limit.

`true` keeps all five stages; a name or list keeps some; `[]` keeps only the progress file. Unset or `false` disables the feature. Each stage writes `<NN>_<stage>.odb`, `.def` and `.sdc` into `artefacts/<run>/checkpoints/<timestamp>-<pid>/`, and `checkpoints/latest` points at the newest run. The `global_route` checkpoint adds the route guides (`.guide`) and global-route segments (`.segments`). The `detailed_route` checkpoint keeps a copy of each report the detailed router wrote, `<NN>_detailed_route.route.drc.rpt` and `.route.maze.log`. The SDC is written with `write_sdc -no_timestamp -digits 9`.

- `tail -f artefacts/<run>/checkpoints/latest/progress.jsonl` follows a running flow. After a kill, the last `step_begin` with no `step_end` is the step the run was in.
- With [`hooks:`](#run-tcl-hooks) set, `hook_begin` and `hook_end` events bracket each hook file and name its point and path. `hook_end` adds the status, the elapsed seconds and, on failure, the error. A `hook_begin` with no `hook_end` is the hook the run was in; rb's retained-checkpoints message names it as the step `hook <point>`.
- Each stage's wall time, CPU time and peak memory are recorded; see [Stage resources](#stage-resources).
- A checkpoint is never final. `rb power` and a plain `rb pnr-export` never read it.
- Old run directories are not pruned. Delete the ones you no longer need.

To look at a checkpoint, open its ODB in OpenROAD or stream it out with [`rb pnr-export --checkpoint`](#export-a-saved-result). To continue from one, the run must be staged; see [Resume a staged run](#resume-a-staged-run).

## Run stages as separate sessions

Set `stages: true` on a run to run each stage in its own OpenROAD session, one after another:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    stages: true
```

The stages are `floorplan`, `place`, `cts`, `global_route`, `detailed_route` and `finish` (fill, parasitic extraction, the `before-reports` hooks, the final reports and the outputs). Each session but `finish` ends by writing its stage's [checkpoint](#keep-stage-checkpoints), which the next session reads, so a staged run writes every checkpoint. `stages: true` with a `checkpoints:` list that leaves a stage out is a config error.

A staged run's checkpoint directory also holds each session's script and log, `<NN>_<stage>.tcl` and `<NN>_<stage>.log`, and with each checkpoint a `<NN>_<stage>.state.tcl` of the flow variables a later stage reads (the `floorplan.macros` cell halos). The run keeps one `progress.jsonl` and one `manifest.json`. `artefacts/<run>/pnr.log` is the sessions' logs in order, written as each session runs, and `pnr.tcl` is their scripts in order.

Each session after `floorplan` starts with a prelude rb generates from the same flow template, so a staged run and a one-session run of the same configuration run the same flow commands. Their results are identical at equal thread counts unless OpenROAD carries in-memory state over a stage boundary in the one-session run; see [Quirks & Known Issues](../known-issues.md#a-staged-pr-run-can-differ-from-a-one-session-run). The prelude is:

1. The header variables, `threads:`, `platform-tcl`, the Liberty of every corner and every LEF, including the views of `blocks:`.
2. `read_db` and `read_sdc` of the previous stage's checkpoint, and its saved flow variables.
3. `max-fanout`, `layer-rc-tcl`, the don't-use cells and the `session` hooks.
4. The flow's design variables (`$block`, `$macros`) and, after `place`, the detailed-placement options a rotated macro needs.
5. `set_propagated_clock [all_clocks]` after `cts`, the routing layers and layer adjustment after `global_route`, for `detailed_route` the `global_route` checkpoint's segments (`read_global_route_segments`), and for `finish` a fanout check, without which OpenROAD crashes in the final reports' max-fanout count.

The detailed router reads the route guides the database keeps, so a staged run routes exactly as a one-session run does; `read_guides` would rewrite them without their via layers. The `finish` session also takes the global route from those guides, as `rb power` does from a routed database. A routed database's pin access points can put a pin in another global-routing cell than the route reached, so OpenROAD would refuse the saved segments there. On a PDK with `rcx-rules` the final reports are timed on the extracted SPEF.

A session has what the previous stage's checkpoint holds and what its prelude sets up again, nothing more:

- The database keeps the design with its global-connect rules, placement blockages, routing layer range and layer adjustments, `dont_touch` marks, track grids and route guides.
- The checkpoint SDC, written at the end of the stage after its hooks, keeps the timing constraints, including `set_max_fanout` and propagated clocks.
- The state file keeps the flow variables named above.

Anything else an earlier session set is gone: Tcl variables, procedures and `rename` wrappers, `set_wire_rc`, `set_layer_rc` and `set_dont_use` that a hook ran, STA settings outside the SDC, and the resizer's in-memory state. In every session but `finish`, rb compares the global Tcl commands and procedures before and after each hook but a `session` one. A hook that renamed, deleted or redefined a command that existed before it logs the warning `pnr.hook_commands_not_kept` naming them, since later sessions run the original. A hook that only defined new commands logs them at info as `pnr.hook_commands_added`: a later session that calls one fails with an unknown command. See [Quirks & Known Issues](../known-issues.md#session-state-does-not-cross-a-pr-stage-boundary).

A session that fails, prints an OpenROAD `[ERROR ...]` line or exits before it ends its stage fails the run with its stage in the description, and no later stage runs. Its stage is recorded as not finished cleanly, so no resume can start after it. A checkpoint that cannot be written fails the run with `fail_stage: checkpoint`, and a session that exits with code 0 before its end-of-stage line fails it with `fail_stage: incomplete`; `xfail:` excuses neither.

### Stop after a stage

`--stop-after <stage>` ends a run, staged or not, once that stage's checkpoint is written:

```bash
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --stop-after global_route
```

Any stage but `finish` can be named. The run writes the stage's checkpoint even without `checkpoints:`, prints a stage report and then `>>> STOPPED after <stage>`. The report gives the design area, setup and hold worst slack and TNS (per corner on a multi-corner platform), timed on no parasitics after `floorplan`, the placement estimate after `place` and `cts`, and the global-route estimate after `global_route` and `detailed_route`.

The result is a `PASS` with `routed: false` and `stopped_after: <stage>`, with `area_um2`, the slack and TNS fields from the report, `global_route_overflow` (the total overflow in the last global-route usage table) from `global_route` on, and `drc_count` after `detailed_route`. A stopped run writes no routed DEF, ODB, SPEF or GDS, publishes no abstract for `harden: true`, and skips the [routed-floorplan check](#check-the-routed-floorplan) and the stream-out. A run that exits with code 0 before printing its stop line fails with `fail_stage: incomplete`, and one that cannot write the stage's checkpoint fails with `fail_stage: checkpoint`. A run's `xfail:` marker does not apply to a stopped run, which is no verdict on the routed design: its `PASS` stays `PASS`, never `XPASS`, and a `FAIL` stays `FAIL`.

### Resume a staged run

`--resume <run> --from <stage>` starts a new staged run at `<stage>` from a staged run's checkpoint of the stage before it:

```bash
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --stop-after global_route
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --resume latest --from detailed_route
```

`<run>` is `latest` or a run id under the run's `checkpoints/`, both looked up there first, or else a checkpoint run directory, which a relative path names from the directory `rb` was invoked in. The resumed run writes its own run directory and leaves the old one unchanged, so detailed routing can be retried several times from one global route. Its manifest records `resumed_from` (run id and stage) and lists the inherited stages' scripts, logs and checkpoints with the run that wrote them (`rb pnr-export --checkpoint <run-id>/<stage>` on the resumed run exports an inherited checkpoint from the run that wrote it, wherever that run is), and its `pnr.log` starts with the inherited stages' logs. A run resumed from `finish` puts the inherited route's `route.drc.rpt` and `route.maze.log` back in the artefact directory, so its `drc_count` and DRC report are that route's. A resumed run is staged whatever its `stages:` setting, and can itself be resumed. Both options need a single named run, and `--stop-after` may be added.

The resume is refused, with `fail_stage: resume` and a description that names what differs, unless:

- the run named was a staged run, and its stage before `--from` finished cleanly with its checkpoint written and unchanged since;
- the script of every stage before `--from`, generated from today's configuration, is the one that run used, as compared below;
- every input file those stages read has the same content; and
- OpenROAD reports the same version.

The input files are those the [checkpoint manifest](#keep-stage-checkpoints) fingerprints: the netlist, the SDC, the Liberty of every corner, the LEF, `pin-constraints`, the planner files, the `pdn-config`, the PDK Tcl hooks (`platform-tcl`, `layer-rc-tcl`, `tracks-tcl`, `tapcell-tcl`) and the run's `hooks:` files. A file that one of them sources or reads in turn, such as a file a hook, `platform-tcl`, a `pdn-config` or the SDC sources, is not checked, and neither are environment variables.

A stage script is compared up to its end-of-stage line, without:

- the two lines that name run directories (`RB_RUN_DIR` and `RB_PRED_DIR`);
- the thread count (`set_thread_count`); and
- each `set NAME value` line of the header at the top of the script whose variable the rest of that stage's script does not read, as `$NAME`, `${NAME}`, `$::NAME` or `${::NAME}`, itself or through another header line it keeps.

Everything else is compared, including rb's own Tcl procedures, so an rtl_buddy version that generates a different script for an earlier stage refuses the resume. The scan sees only the stage script: a hook file, `platform-tcl` or other file that the stage sources and that reads a header variable does not make that variable part of the comparison.

So a setting that only `--from` and later stages read may change between the runs: `detailed-route-verbose:` or a `before-detailed-route` hook, the routing layers (first read by `global_route`), or the fill cells (read by `finish`). The thread count may differ between stages, so a resume can run on another CPU allocation than the run it resumes; a staged run equals a one-session run of the same configuration only at equal thread counts.

A resume is checked before anything is cleared or written. A refused resume, or one that fails setup before it is checked, leaves `checkpoints/latest` and the previous run's outputs as they were, so the same `--resume latest` can be retried once the configuration is put back.

### Dispatch the stages to Slurm

`--dispatch slurm` runs each stage of a staged run as its own Slurm job:

```bash
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --dispatch slurm
```

rb writes every stage's script into the run directory first, so the jobs run the configuration as it was when the run started. It then submits one job per stage, each held until the one before it succeeds (`--dependency=afterok`), and waits until the last one has left the queue. Each job runs its stage as a local staged run does and adds to the same `pnr.log`, `progress.jsonl` and checkpoints, so the result, manifest and outputs are those of the same run staged locally. `--dispatch slurm` makes a run staged whatever its `stages:` setting. With `--stop-after` or `--resume` it submits only the stages the run runs. `--dispatch local`, the default, runs the stages where `rb pnr` runs; `cfg-dispatch.backend` does not apply to `rb pnr`.

Each job's reservation is resolved field by field, most specific first, from the run's `resources.stages.<stage>`, the run's `resources:`, `cfg-dispatch.resources`, then the built-in defaults:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    threads: auto
    resources:
      cpus: 4
      mem: 8G
      time: "02:00:00"
      stages:
        detailed_route: {cpus: 16, mem: 32G, time: "12:00:00"}
```

`threads: auto` takes each job's CPUs, so here detailed routing runs on 16 threads and every other stage on 4. A fixed `threads:` count is the same in every stage, clamped to the CPUs of a smaller job. Each job counts the CPUs it received when it starts, as a run inside an allocation does (see [OpenROAD threads](#openroad-threads)), so a `--cpus-per-task` in `cfg-dispatch.sbatch-args` or a site that grants a different count sets the thread count too. A run that is not dispatched ignores `resources:`. A dispatched run without a `resources:` block warns (`pnr.dispatch_resources_default`) with the CPUs, memory and time every job then gets.

Every job asks for one task (`--ntasks=1`); `cfg-dispatch.sbatch-args` come after the reservation and apply to every job. The jobs are named `rb:pnr:<run>:<stage>`, tagged by `RTL_BUDDY_JOB_TAG` (see [Tag job names for one caller](dispatch.md#tag-job-names-for-one-caller)). The console shows each job id as Slurm accepts it, then the stage `rb pnr` waits on and its state.

`rb pnr` itself stays on the submitting host. It writes the scripts, submits the jobs and waits for them, then runs there every check a local run makes on the routed result. The stream-out for `--gds`, `--png` or `harden: true` also runs there: KLayout reads the whole routed layout, so give the submitting host, or the allocation `rb pnr` runs in, memory to match.

A stage whose job fails, is cancelled, runs out of time or is killed for memory fails the run, and Slurm removes the jobs after it. The description names the stage and how the job ended, for example `stage 05_detailed_route: OpenROAD exited with code -9 (Slurm job 1234 ended OUT_OF_MEMORY)`. A job killed inside its stage gets the stage's `stage_end` from Slurm's accounting, and the stage's log is cut from `pnr.log` when the job had not written it. A stage succeeds only when its job wrote a successful result and Slurm's accounting records the job `COMPLETED`, so a job that Slurm ended after its stage succeeded, for example when its `time` ran out as it exited, fails the run at that stage. When accounting gives no end state for a job, its result is taken as written, with the warning `pnr.stage_jobs_unaccounted`.

`cfg-dispatch.max-wait` bounds how long each job waits in the queue before it starts: the first from its submission, each later one from when the job before it ends. How long a job runs is bounded by its own `time`. A job that waits longer, or one that cannot be submitted, fails the run and cancels the jobs; `sbatch` gets 10 minutes to answer a submission before it is stopped and the submission fails. The wait counts while `squeue` does not answer, and each `squeue` of the wait gets at most 30 seconds, so one that hangs counts as not answering. A job past `max-wait` then is given up only when Slurm's accounting (`sacct`) records it as still pending; when accounting has no answer either, only once it has been queued longer than `max-wait` plus its own `time`, so a job that started while the queue could not be read is never cancelled as unstarted.

A failed stage carries its own `fail_stage` only when its job ran the stage and ended itself; it then fails as it would in a local staged run. Every other failure is the scheduler's and carries `fail_stage: dispatch`, which `xfail:` never excuses, as for an `rb test` job the scheduler lost:

| How the stage's job ended | `fail_stage` |
| --- | --- |
| Ran its stage, which failed, and ended `COMPLETED` or `FAILED` | The stage's own: none, `incomplete` or `checkpoint` |
| Ran its stage, which succeeded, and ended in any state but `COMPLETED` | `dispatch` |
| Ran its stage and ended in any other state: `OUT_OF_MEMORY`, `TIMEOUT`, `CANCELLED`, `NODE_FAIL`, `PREEMPTED` | `dispatch` |
| Refused its stage, never started it, or left no result | `dispatch` |
| Could not be submitted, or waited in the queue longer than `max-wait` | `dispatch` |
| Refused because an earlier dispatched run's jobs are still queued (below) | `dispatch` |

Ctrl-C, SIGTERM or SIGHUP cancels the run's own jobs, and no others, including a job whose submission is still waiting on `sbatch`. `rb pnr` then waits up to 30 seconds for them to leave the queue, closes the stage a cancelled job was running as for a killed job, finishes the manifest and exits. A second Ctrl-C ends that wait early, in every run under `-j`; one that arrives while Slurm is asked to cancel takes effect once the request is done, and each `squeue` and `scancel` of the cancel gets at most 10 seconds. Under `-j`, every run being dispatched stops the same way and no further run starts. Jobs still queued after the wait are named with the `scancel` command that cancels them. Any other error before the chain has left the queue, such as a full disk, cancels the jobs and finishes the manifest the same way before rb reports it. A signal that arrives after the chain has left the queue takes effect once rb has read how the chain ended; the run then fails as interrupted.

An `rb pnr` killed outright (SIGKILL, or the end of the allocation it runs in) leaves its jobs to run the rest of the chain unattended: the checks it makes after the last job never run and its manifest is never finished. While a job of an earlier dispatched run of the same configuration is queued or running, every `rb pnr` of it, dispatched or not, refuses to start before it clears anything, naming the jobs and the `scancel` command. It looks up only your own jobs, and refuses too while `squeue` does not answer, since it cannot tell. On a host without `squeue`, a run that is not dispatched refuses while the earlier run's `dispatch/` has changed within the total `time` its jobs asked for; after that it warns (`pnr.stage_jobs_unchecked`) and runs. That age is no proof that the jobs have ended, since a job can wait in the queue for any time before it starts. A submission that `sbatch` has not answered yet, such as one that was running when its `rb pnr` was killed, has no job id to look up, so it refuses every `rb pnr` of the configuration, naming its `dispatch/<NN>_<stage>.sbatch.tmp` (or `.sbatch.cancel`), until it is 11 minutes old: `sbatch` is stopped after 10 minutes, so a submission with no answer for longer has failed, and `rb pnr` warns (`pnr.stage_submission_presumed_failed`) and runs. Once the jobs have ended, or the submission is known to have died, removing the earlier run's `checkpoints/<run id>/dispatch/` directory lets `rb pnr` run without looking them up; a refusal that could not confirm the jobs names the directory. A job refuses its stage when its run has already finished, or when it runs another rtl_buddy version than the `rb pnr` that started the run.

The run directory's `dispatch/` holds `jobs.json`, with each stage's job id, dependency and reservation and whether all have left the queue, and for each stage Slurm's answer to the submission (`<NN>_<stage>.sbatch`, `<NN>_<stage>.sbatch.tmp` until `sbatch` answers), the job's Slurm output (`<NN>_<stage>.slurm.log`), its rtl_buddy log (`<NN>_<stage>.rtl_buddy.log`) and how it ended (`<NN>_<stage>.result.json`). A job whose CPUs give another thread count than the head wrote runs a copy of the stage script with that count, `<NN>_<stage>.tcl` there; the stage script in the run directory stays as the manifest records it. The manifest's `dispatch` entry records the backend and, for each stage, its reservation, `threads:` and the thread count that reservation gives. Each stage's `stage_begin` and `stage_end` record the count its job set (`thread_plan`), and the run's `openroad_threads` result is the last stage job's.

### Stage resources

Each stage's wall time, CPU time (user plus system) and peak resident memory are recorded as `stage_end` events in `progress.jsonl` and under `resources` in the manifest, with `measured_by` naming how:

- In a staged run rb measures each session as a process (`measured_by: process`) and also records its thread count (`threads`, the count OpenROAD reported, else the one `threads:` set). A [dispatched](#dispatch-the-stages-to-slurm) stage is measured the same way inside its job, and its `stage_begin` and `stage_end` name the job (`slurm_job`) and `host`. A job killed inside its stage gets a `stage_end` with Slurm's accounting (`measured_by: scheduler`, and the job's `slurm_state`).
- In a one-session run with `checkpoints:` or `--stop-after`, the flow measures each stage from inside OpenROAD (`measured_by: session`) at the stage boundaries of the [checkpoint table](#keep-stage-checkpoints): wall time from the Tcl clock, CPU time from `/proc/self/stat`, and peak memory from `VmHWM`, reset at each stage boundary through `/proc/self/clear_refs`. The reset starts from the memory the process already holds, so a stage's peak includes what earlier stages left resident (the `finish` stage after detailed routing, for example). Without `/proc` the CPU time and peak memory are `null`, and the peak memory is `null` after the first stage when the reset is refused.

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
- The run also elaborates the block's top in Yosys, with the frontend, sources, defines and Liberty of the block's synthesis, and records the values of its parameters and localparams as `<top>.params.json`. Only names the top declares in its own scope are recorded: a generate loop's genvar, or a localparam inside a generate block, function or procedural block, is not. Parents' overrides are checked against it (see [Instance a parameterised block](#instance-a-parameterised-block)). The probe's script, log and output stay in `param_probe/` of the artefact directory until the next run. If Yosys cannot elaborate the top, the abstract is published without the record and the run warns `pnr.block_params_unrecorded`.
- The views are published together or not at all. If one cannot be produced, the run fails with `fail_stage: abstract` and leaves no `abstract/`; the routed DEF and ODB stay. Each rerun removes the previous abstract first.
- `harden` turns on `buffer-ports`: `buffer_ports -inputs -outputs` runs after IO pin placement and before global placement, as ORFS `global_place.tcl` does, and `repair_design` sizes the buffers. Each input then drives one buffer instead of its internal loads, and each output is driven by one, so the abstract's pin capacitance and drive do not depend on the block's internals. Without it, an input fanning out to a thousand loads carries their whole capacitance into the Liberty model for the parent to drive. OpenROAD leaves clock, constant and special nets unbuffered. The platform's `port-buffer` names the buffer cell; unset, OpenROAD picks one, and on sky130hd it picks the delay cell `sky130_fd_sc_hd__clkdlybuf4s50_1`, so set `port-buffer` there (ORFS uses `sky130_fd_sc_hd__buf_4` as its minimum buffer). Set `buffer-ports: false` to turn it off, or `buffer-ports: true` on a run that does not harden.
- `buffer-ports` is part of the block's configuration only when on, and `port-buffer` only when it is set and ports are buffered, so an abstract hardened by an rtl_buddy without it is stale (`config changed (buffer_ports)`) until the block is re-hardened, and one hardened with `buffer-ports: false` is not.
- An abstract carries one corner's timing model, so `harden` on a multi-corner platform is refused before OpenROAD starts.
- The model has timing arcs only, so a parent's `rb power` sees the block as drawing 0 W. Run `rb power` on the block itself.

### Choose the abstract's obstruction style

By default the abstract LEF is written with `write_abstract_lef -bloat_occupied_layers`: every layer the block uses is an obstruction over its whole footprint. A block with supply straps on met4 then blocks met4 everywhere, and a parent can cross it only on met5. `abstract:` on the hardened run chooses otherwise:

```yaml
runs:
  - name: alu_block_pnr
    harden: true
    abstract:
      bloat: occupied            # default; or a number, OpenROAD's -bloat_factor
      feedthrough-layers: [met4] # these layers get the block's real shapes
```

- `bloat: <number>` writes `-bloat_factor <number>` instead: the block's shapes grown by that factor, so a parent can route between them on every layer. `0` writes the real shapes.
- `feedthrough-layers` keeps the `bloat` style on every other layer and writes the listed ones with the block's real shapes as obstructions. rtl_buddy writes a second LEF with `-bloat_factor 0` and splices its obstructions on those layers into `<top>.lef`. A listed layer the block does not use has no obstruction; one that appears in neither LEF, likely a misspelling, warns `pnr.abstract_feedthrough_layers_unused`.
- `abstract:` needs `harden: true`.
- It is part of the block's configuration only when it differs from the default, so abstracts hardened before it existed stay current, and changing it makes the abstract stale (`config changed (abstract)`) until the block is re-hardened. A hand-edited LEF is still stale.
- With fewer obstructions a parent's router can route over the block. Check the parent's DRC and the block's pins and straps after changing the style.

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
- **Overrides match the elaborated block, as far as the record goes.** Every `harden: true` run elaborates the block's top once more in Yosys (scratch files in `param_probe/` of the run's artefact directory) and records its parameter values as `<top>.params.json` in the abstract. Each override on an instance, named or positional, is compared with its recorded value as a whole number; two literals of the same width compare by their bits. A real, recorded as Yosys's string (`"2.500000"`), compares numerically with the netlist's `2.500000`. All instances of a block must also agree. What the record covers depends on the block's frontend:
  - **`frontend: slang`**: the record lists the top's parameters and localparams, and no genvars or nested-scope localparams, so an override the block does not have fails. A real-valued parameter cannot be recorded; the abstract is then published without a record.
  - **native `verilog` frontend**: the record lists parameters but not localparams, so an override the record lacks is only a warning. A `real` parameter is recorded only when the synthesis's `params:` sets it.
  - **No record**: an abstract hardened by an older rtl_buddy, or one whose top Yosys could not elaborate (warning `pnr.block_params_unrecorded`). Only the synthesis's `params:` are compared, with a warning to re-harden. A YAML integer, or a quoted unsized number such as `"-1"`, matches an override of width W when it fits W bits, signed or unsigned, and agrees modulo 2^W.
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
- **`fail_stage: routed_check`.** A located macro or exact pin in the routed DEF is not where the floorplan put it. A `pin-constraints` file or a platform hook that moves it is the usual cause. See [Check the routed floorplan](#check-the-routed-floorplan).
- **`fail_stage: pins_complete`, `floorplan.pins-complete: ...`.** Add a pin entry for each port listed, or remove the duplicate entries. See [Check that every port is placed](#check-that-every-port-is-placed).
- **`floorplan.macros: the netlist has no hard macros, ...`.** The netlist has no instance of a LEF `CLASS BLOCK` master, so the located entries place nothing. Check that synthesis kept the macro instances. See [Place individual macros](#place-individual-macros).
- **`PDN-0008 ... halo overlaps row`.** A macro grid's `-halo` in the `pdn-config` reaches into a row beside the macro, which OpenROAD 26Q4's `pdngen` refuses. Reduce the halo to at most the value the message suggests.
- **`block power: ... is not on a parent <net> strap`, `no single parent power net`, or `PDN-0233` on a block's macro grid.** A block's supply pins are off the parent's straps or cannot be tied. See [Blocks with supply pins on the top strap layer](#blocks-with-supply-pins-on-the-top-strap-layer).
- **`instance '<inst>' ... of block '<blk>' sets <P>=...`, `port '<p>' is connected to N bit(s)`, `instances of block '<blk>' have different parameters`, or `... named by a hash`.** The parent instances a block with parameters or ports it was not hardened with. See [Instance a parameterised block](#instance-a-parameterised-block).
- **`fail_stage: checkpoint`.** A stage checkpoint that a staged run or `--stop-after` needs could not be written (the `checkpoint` event in `progress.jsonl` has the error), or a one-session run never ended the stage the description names because a hook wrapped its command with `rename`. Free disk space, or remove the wrapper or set `stages: true`. See [Keep stage checkpoints](#keep-stage-checkpoints).
- **`fail_stage: dispatch`.** A [dispatched](#dispatch-the-stages-to-slurm) stage's job was ended by Slurm, refused or never started its stage, or could not be submitted or waited in the queue past `max-wait`. The description names the stage, the job and its Slurm state; the job's `dispatch/<NN>_<stage>.slurm.log` has its output. Raise the stage's `resources:` for `OUT_OF_MEMORY` or `TIMEOUT`. A run refused before it started names the jobs of an earlier dispatched run that are still queued: wait for them, or cancel them with the `scancel` command it prints. When it could not look them up (`squeue` did not answer, or the host has none), run where `squeue` answers, or remove the `dispatch/` directory it names once the jobs have ended. When it names a submission `sbatch` has not answered, run again once `sbatch` answers, at most 11 minutes after the submission began, or remove the `dispatch/` directory it names once the submission is known to have died.
- **`fail_stage: resume`.** `--resume` was refused; the description names the stage script, input file, checkpoint or OpenROAD version that differs. Rerun the stages from that point, or resume from an earlier stage. See [Resume a staged run](#resume-a-staged-run).
- **`fail_stage: error`.** The run crashed; the exception is in the row description. Its consumers are blocked and other runs still report.
