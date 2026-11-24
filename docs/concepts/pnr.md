---
description: Run OpenROAD place-and-route on a synthesised netlist with rb pnr, read and debug the result, and control the floorplan, timing repair, long runs, GDS export and hardened blocks.
---

# Place-and-Route

`rb pnr` places and routes a netlist from `rb synth` with OpenROAD. It reports area, timing, routing DRCs and electrical violations. This page says when and why to use each setting; [YAML Formats](../reference/yaml.md#pnryaml) defines every `pnr.yaml` and platform field.

## Terms

| Term | Meaning |
| --- | --- |
| PDK | Process design kit: one process's cell libraries, layer rules and layouts, named by a `cfg-pdks` entry. |
| Platform | A `cfg-pnr-platforms` entry: a PDK, its corners and P&R settings. |
| Corner | A process, voltage and temperature condition with its own Liberty timing, such as `ss`. The primary corner is the platform's first. |
| Liberty, LEF, DEF, ODB, GDS | Cell timing models; cell and layer outlines; a placed design as text; OpenROAD's database; the final layout. |
| SDC, SDC mode | The timing constraints file. A mode is one set of constraints that OpenSTA times on its own. |
| Slack, WNS, TNS | Slack is required minus arrival time; negative misses timing. WNS is the worst slack, TNS the sum of negative endpoint slacks. Setup checks the latest arrival, hold the earliest. |
| Slew, electrical violator | Slew is a signal edge's transition time. An electrical violator is a pin over its max-slew, max-capacitance or max-fanout limit. |
| DRC | Design rule check. A DRC violation is a routed shape that breaks a layer rule. |
| Parasitics, wire RC | Wire resistance and capacitance: estimated from placement or global route with a per-layer wire RC, or extracted from the detailed route into a SPEF file. |
| CTS | Clock-tree synthesis: building the buffered clock networks. |
| Global route, detailed route | Global route plans nets over a grid of GCells; overflow is demand above capacity. Detailed route draws the wires. |
| ECO | Engineering change order: rerouting only the nets a repair changed. |
| Legalization | Moving cells onto free, non-overlapping row sites after a step adds or moves them. `detailed_placement` does it. |
| Macro, RTL-MP | A block with a fixed layout, such as an SRAM or a hardened block. RTL-MP is OpenROAD's macro placer. |
| `repair_design`, `repair_timing` | OpenROAD's repairs: the first fixes slew, capacitance, fanout and long wires, the second setup or hold. |
| ORFS | OpenROAD-flow-scripts, the reference flow whose settings rb follows. |
| Stage, session | A stage ends at a checkpoint: `floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `post_route_repair`, `finish`. A session is one OpenROAD process. |
| Abstract | The LEF, Liberty and GDS a hardened block publishes for a parent. |

## Run P&R on a block

You need OpenROAD 25Q1 or newer on `PATH` (or named in `cfg-pnr-tools`) and a synthesis run that has written its netlist. An older OpenROAD logs a warning and runs unvalidated. A version that does not start with a release number, such as `HEAD-HASH-NOTFOUND` from a build of an untagged tree, skips the check, and such a build cannot [resume](#run-stages-as-separate-sessions) a run. KLayout is needed only for GDS and PNG output.

Add a P&R platform to `root_config.yaml`. It picks a PDK from `cfg-pdks` (see [Synthesis](synthesis.md#configure-tools-and-the-pdk)), a corner and the clock-tree buffer:

```yaml
cfg-pnr-platforms:
  - name: nangate45_typ
    pdk: nangate45
    corner: typ
    cts-buffer: BUF_X4
    routing-layers: {signal: metal2-metal8, clock: metal4-metal8}
```

Describe the run in a `pnr.yaml`. Paths are relative to that file. The top module comes from the synthesis entry.

```yaml
rtl-buddy-filetype: pnr_config
runs:
  - name: alu_pnr
    desc: ALU at the typical corner
    synth: alu_synth
    synth-path: ../../synth/alu/synth.yaml
    constraints: ../../synth/alu/alu.sdc
    platform: nangate45_typ
    floorplan: {utilization: 0.55, aspect: 1.0, core-margin: 2.0}
```

```bash
rb pnr --list -c pnr/alu/pnr.yaml
rb pnr alu_pnr -c pnr/alu/pnr.yaml
rb pnr alu_pnr -c pnr/alu/pnr.yaml --png    # also stream out GDS and render a PNG
```

- Outputs go to `artefacts/<run>/` next to `pnr.yaml`.
- Each run first deletes the previous run's outputs, except a refused `--resume`, which stops before that.
- With no run name, `rb pnr` runs every entry whose `reglvl` is within `-l`, hardened blocks before the runs that use them.

## Interpret results

The verdict says whether the flow finished; the fields say whether the design is good.

| Verdict | When |
| --- | --- |
| `PASS` | OpenROAD ran the flow to its end and wrote its outputs. A `mode: probe` or `--stop-after` run passes at the stage it stops after, with `routed: false` and no routed outputs. |
| `FAIL` | OpenROAD exits non-zero, logs an `[ERROR ...]` line (except in a post-route repair session rb sets aside or a [hook](#run-tcl-hooks) that returns) or stops before the end of its flow, or a check fails. |
| `SKIP` | `-l` filters the run out, or `tool:` is not `openroad`. |

Check `routed` before treating a `PASS` as routed. A `PASS` does not mean the design meets timing or routes clean unless [signoff gates](#gate-signoff) are set. Judge the design on these fields:

| Field | Meaning |
| --- | --- |
| `wns_setup_ps`, `wns_hold_ps`, `tns_ps`, `tns_hold_ps` | Worst and total negative setup and hold slack. |
| `drc_count` | Violations left in `route.drc.rpt`; 0 is a clean route. |
| `max_slew_violation_count`, `max_capacitance_violation_count`, `max_fanout_violation_count` | Pins over a Liberty or SDC limit at any corner, listed in `electrical.rpt`. |
| `worst_path_startpoint`, `worst_path_endpoint`, `worst_path_levels`, `worst_path_resolved` | The worst setup path over all path groups at the setup-critical corner: its ends as RTL names, its combinational cells, and whether both ends resolved. See [below](#find-the-critical-path-and-loaded-nets-in-the-rtl). |
| `max_net_fanout` | Sink pins of the highest-fanout signal net, excluding clock, reset-tree and supply nets. |
| `macro_pin_buffered_count`, `macro_pin_unmet_count` | With `placement.macro-pin-limits`: pins it buffered, and pins still over their limit less the margin on placement parasitics. |
| `area_um2` | Design area. |
| `routed_cell_count` | Instances of the finished design, with added tie, buffer and clock cells and without physical-only cells. Compare experiments on this. |
| `cell_count` | Instances of the input netlist (the summary's Cells column). |
| `physical_cell_count` | Tap, endcap, spacer and fill cells. |
| `worst_setup_corner`, `worst_hold_corner`, `corners` | Multi-corner: the corner behind each WNS, and each corner's four timing values. |
| `routed`, `mode`, `stopped_after`, `global_route_overflow` | `routed: false` is a run that stopped early; its numbers are that stage's estimate, with the overflow from `global_route` on. |
| `signoff` | Each [signoff gate's](#gate-signoff) values and verdict. |
| `post_route_repair` | [Post-route repair](#repair-timing-after-routing): `kept_round`, `rounds`, `repaired_rounds`, `stop`, `reason`, `passed_over_round`, and `electrical_round`, whose `unseen` lists the violators the kept electrical round's `repair_design` judged within limits on the placement estimate. |
| `openroad_threads` | Requested and effective [thread counts](#openroad-threads). |
| `gds_status`, `gds_missing_cells` | [Stream-out](#stream-out-gds-and-png) outcome. |
| `checkpoint_dir`, `checkpoint_stages`, `last_step` | [Checkpoint](#keep-stage-checkpoints) location, the stages checkpointed (never a failed global route) and the last step reached. |
| `failed_global_route` | `<run-id>/global_route`, the checkpoint holding a failed global route, for `rb pnr-export --checkpoint`. |
| `allow_congestion` | `true` on a [diagnosis run](#tune-global-routing); `global_route_overflow` is then the final overflow. |

Times are picoseconds, converted from the Liberty `time_unit`. Final timing uses extracted parasitics when the PDK sets `rcx-rules`, else the global-route estimate. A field rb could not read is left out.

A passing run's description can carry these qualifiers:

| Qualifier | Meaning |
| --- | --- |
| `pnr.no_wire_rc` | Placement and CTS ran without [wire RC](#set-wire-rc). |
| `electrical N max-slew, ... violator(s)` | Electrical violators remain. `fail-on-electrical: true` or `electrical` in `signoff.fail-on` fails such runs. |
| `post-route repair ...` | A repair round was passed over, or its failed session was set aside. |
| `post-route electrical round N kept`, `not kept`, `failed` | The [electrical round](#repair-electrical-violators-after-routing) ran. |
| `GDS incomplete: ...` | Cells without layout, in `preview` mode. |
| `stale block abstract(s) accepted: ...` | `--accept-stale` was used. |
| `global-route.allow-congestion (diagnosis only): global route overflow N` | Global route kept its [overflow](#tune-global-routing). |

### Find the critical path and loaded nets in the RTL

The final reports write `worst_paths.rpt` and `fanout.rpt`, which name RTL signals so you can find the slow path and the heavily loaded nets in your source.

`worst_paths.rpt` gives each path group's worst setup path: its slack, logic levels, startpoint and endpoint. `timing.rpt` holds the full path.

- Path groups are one per capture clock, `asynchronous` for recovery and removal checks, and any `group_path` groups.
- Logic levels count the cells between the startpoint's and endpoint's cells, neither counted, buffers and inverters included; the file says how many are buffers or inverters.
- On a multi-corner platform every group is timed at the setup-critical corner (worst setup slack), which the file names. With an SDC mode per corner, the groups are that corner's mode's.

`fanout.rpt` lists the `report-fanout-nets` (default 10) highest-fanout signal nets, highest first, with their drivers.

- Fanout is the net's sink pins: instance inputs and output ports. A pin count compares across PDKs, where delays do not.
- It excludes supply nets, clock nets (the STA clock network and nets CTS built) and reset-tree nets, which reach a register's asynchronous set or clear pin through any buffers and inverters.

Each point and driver gets its RTL name from the first of these the netlist keeps:

1. A top-level port, by its name.
2. A pin of an instance the netlist names itself, such as a macro or hardened block: `u_core.u_sram.din0[3]`.
3. The net that names the pin, when the netlist declares it under a name of its own:
   - for a startpoint or driver, the net its pin drives;
   - for an endpoint, the register's output when the cell has exactly one non-clock Liberty clock-to-output arc, so it reads as the register (`u_core.state_q[3]`); otherwise the net on the endpoint pin.
4. An instance with a Yosys `src` attribute, by scope and source lines: `u_core (alu.sv:42)`.

- Yosys-made names (`_123_`, any name with `$`) are not the netlist's own.
- A driver P&R inserted is traced back through buffers and inverters to the first driver that resolves. The file says how many cells back, and `inverted` when an odd number are inverters.
- An unresolved name stays as OpenROAD prints it, marked `(unresolved)`. The file gives the raw name and `src` beside every resolved one.
- `worst_path_resolved` is `false` unless both ends resolve.
- If the reports cannot be written, `pnr.path_report_failed` warns and their fields are left out; the verdict does not change.
- A value that cannot be computed is left out of the result and is `unavailable` in its file with the reason, never 0. A report whose commands failed part way lists nothing.

The reports add about 0.4 s to the final reports on a routed design of 373,000 instances and 60,000 nets. The fanout scan grows with the net count, the path search with the number of path groups.

## Inspect artefacts

| File in `artefacts/<run>/` | Contents |
| --- | --- |
| `pnr.log`, `pnr.tcl` | OpenROAD output, written live, and the generated flow script. |
| `<top>.def`, `<top>.routed.odb` | Routed design. `rb power` reads the ODB. |
| `<top>.routed.v`, `<top>.routed.sdc` | Netlist without supply ports or supply pin connections, and the primary corner's constraints, for STA and gate-level simulation. |
| `<top>.routed.pwr.v` | Powered netlist for LVS against `<top>.gds`: the top's supply ports, last and sorted by name, and every instance's supply pins (fill and tap cells too), connected by the `pdn-config` global connections. Staged and one-session runs write the same file. |
| `<top>.<corner>.routed.sdc` | Each corner's constraints on a [multi-corner platform](#sign-off-at-several-corners). |
| `<top>.routed.spef` | Extracted parasitics, with `rcx-rules`. |
| `timing.rpt`, `electrical.rpt` | Worst paths and electrical violators, all corners. |
| `worst_paths.rpt`, `fanout.rpt` | [Worst path per group and highest-fanout nets](#find-the-critical-path-and-loaded-nets-in-the-rtl), with RTL names. |
| `route.drc.rpt`, `route.maze.log` | Detailed-route DRCs and router log. |
| `grt_antennas.rpt`, `drt_antennas.rpt` | Antenna violations left after the antenna repairs after global and detailed route; with `post-route-repair`, the kept round's. |
| `congestion.rpt`, `congestion-<i>.rpt` | Overflowing global-route tiles, only when global routing overflows; loads in the OpenROAD GUI's DRC viewer. The `-<i>` files are iteration `i`, every `global-route.congestion-report-iter-step` iterations. |
| `rtlmp/` | RTL-MP's reports, when it placed macros. |
| `<top>.gds`, `<top>.png`, `klayout.*.log`, `def2stream.inputs.json` | Stream-out outputs, logs and inputs read. |
| `checkpoints/`, `abstract/` | [Stage checkpoints](#keep-stage-checkpoints) and a [hardened block's](#harden-a-block) views. |

- When OpenROAD fails, the run removes the routed database so `rb power` never reads a stale one. `pnr.log`, `pnr.tcl` and the congestion reports stay, and a failed global route leaves its database as a [checkpoint](#keep-stage-checkpoints).
- A run that fails a later check (`routed_check`, `fail-on-electrical`, a signoff gate, `export`, `abstract`) keeps its routed outputs.

## Follow a running flow

- `tail -f artefacts/<run>/pnr.log`. Each step starts with a `>>> ` line.
- Global route prints a per-layer usage table (`GRT-0096 Final congestion report`).
- Detailed route logs each iteration (`DRT-0195`) and its violation count (`DRT-0199`), so you can tell converging from stalled. `detailed-route-verbose: 0` silences this; higher levels add detail.
- With [checkpoints](#keep-stage-checkpoints), `checkpoints/latest/progress.jsonl` records each step, hook and stage. After a kill, the last `step_begin` without a `step_end` is where the run was. Its [`stage_timing` events](#compare-timing-across-stages) give each corner's slack at every stage end.

## Fix a failed run

Read the row's description and `fail_stage`, then `pnr.log`, and find the stage or message below. A result with any `fail_stage` failed before or instead of a verdict on the design, so `xfail:` never excuses it. A failure without one (an OpenROAD error, a don't-use violation, `fail-on-electrical`, a [signoff gate](#gate-signoff)) can be excused.

| `fail_stage` | Cause and fix |
| --- | --- |
| `setup` | A configuration or input problem found before OpenROAD started, named in the description: a missing file (`pdn-config`, `rcx-rules`, a hook, a planner file), a bad planner-file entry, `post-route-repair` without `rcx-rules`, blockages on OpenROAD older than 26Q1, `cts-obstruction-aware: false` on an OpenROAD without `-no_obstruction_aware`, a per-corner `constraints:` that does not fit the platform, or no answer to the [SDC mode check](#sign-off-at-several-corners). |
| `incomplete` (`the flow did not finish`) | A sourced Tcl file (PDK Tcl, `pdn-config`, `pin-constraints`, a hook) called `exit` or `return -level 2`, ending OpenROAD with code 0. Remove the exit from the file the named last step sources. |
| `routed_check` | A located macro or exact pin moved after the floorplan, usually by a `pin-constraints` file or hook. |
| `pins_complete` | `pins-complete: true` found a port with no pin entry or conflicting entries. |
| `checkpoint` | A checkpoint could not be written (disk full; see the `checkpoint` event in `progress.jsonl`), or in a one-session run a hook wrapped `global_placement`, `clock_tree_synthesis`, `global_route` or `detailed_route` with `rename`. Free space, remove the wrapper, or set `stages: true`. |
| `resume` | A stage script, input file, checkpoint or OpenROAD version differs. The description lists each script and input difference with its [`--accept-change`](#accept-a-change-on-resume) value; otherwise rerun from an earlier stage. |
| `dispatch` | A Slurm stage job ended `OUT_OF_MEMORY`, `TIMEOUT`, `CANCELLED` or similar, never started, or was refused. Read `dispatch/<NN>_<stage>.slurm.log` and raise that stage's [`resources:`](#dispatch-the-stages-to-slurm). |
| `abstract` | A hardened block's view could not be written. Read `pnr.log`. |
| `outputs` | rb could not read, decode or rewrite `<top>.routed.pwr.v` or a hardened `<top>.lib` to put supply ports last; the description names the file and error. |
| `export` | Stream-out failed (KLayout, `klayout-tech`, an input file, the render) under `gds-mode: strict` in `rb pnr`, or in any mode in `rb pnr-export`. Fix it and run `rb pnr-export`. |
| `blocked`, `synth`, `error` | A block this run uses failed, its synthesis failed under `--synth`, or rb crashed (exception in the description). |

| Message | Cause and fix |
| --- | --- |
| `[ERROR PDN-0179] Unable to repair all channels` | Macros too close for the power grid. Set `placement.macro-channel`. |
| `macro ... overlaps floorplan.blockages[N]` (or `floorplan.core-cutouts[N]`) | A placed macro sits on a `hard` blockage or core cut-out. Locate it with `floorplan.macros`, enlarge `placement.macro-channel`, or move the blockage. |
| `PDN-0008 ... halo overlaps row` | A macro grid's `-halo` reaches a row the macro does not sit on. [Cut the rows](#plan-the-power-grid) that far. |
| `PDN-0233` on a turned macro or block grid | The grid has no via for that macro. See [Place macros](#place-macros) and [Harden a block](#harden-a-block). |
| `GRT-0116`, global-route overflow | Too congested. `failed_global_route` names the checkpoint with the failed route and a copy of `congestion.rpt`; open it with `rb pnr-export --checkpoint` or `read_db`. Lower utilization or density, try `placement.routability-driven`, or [tune global routing](#tune-global-routing). Congestion between macros or at the core edge means RTL-MP left no channel: set `placement.macro-channel`, such as ORFS's `[80, 80]` on sky130hd. |
| Large `drc_count` | Try `routing-layer-adjustment`, a lower density, or `post-route-repair` with several rounds. |
| `RSZ-0090` at a slow corner | A macro with one Liberty limits that corner. [Map `lib-paths` per corner](#sign-off-at-several-corners). |
| Macro pins in `electrical.rpt`, `pnr.macro_pin_limits_unmet` | Set `placement.macro-pin-limits`, add a stronger buffer to it, or lower `macro-cell-halo`. See [Tune placement](#tune-placement-clock-tree-and-repair). |
| Slow output pins at signoff, yet no max-slew violators | Ports have no limit unless the SDC sets one. See [Check and repair slew at output ports](#check-and-repair-slew-at-output-ports). |
| `RSZ-0074` in a post-route repair round | Set `post-route-repair.reroute: full`. |
| `RSZ-0089 Could not find a resistance value for any corner` | Setup repair ran on zero wire RC. [Set `layer-rc-tcl`](#set-wire-rc). |
| `CTS-0080 Sink not found` | Set `cts-sink-clustering: false`; with macros, `cts-obstruction-aware: false` also avoids it. |
| `GRT-0273 Disabled NDR`, large hold violations | Clock nets with non-default rules detour. Set `cts-apply-ndr: none`. |
| `DPL-0011` | A macro off the row grid (`macro-snap: false`). Put its bottom edge on a row. |
| `IFP-0039` at `make_tracks` (ASAP7) | Set the PDK's `tracks-tcl`. |
| `RB-DONT-USE-VIOLATION` | The routed design uses an excluded cell, usually from the netlist. Change the RTL, synthesis or pattern. |
| `STA-0122 cell '<pattern>' not found` | A `dont-use-cells` or `cts-dont-use-cells` pattern matches nothing. |
| `openroad.threads_capped` | `threads:` exceeds the allocation. Raise the reservation or use `threads: auto`. |
| `floorplan.pins[i]: no port matches`, `floorplan.macros[i]: no instance matches` | Fix the name or glob. A macro name is the netlist's, brackets included; see [Place macros](#place-macros). |
| `floorplan.macros: the netlist has no hard macros` | Check that synthesis kept the macro instances. |
| Missing or stale block abstract, platform or corner mismatch | Run the `rb pnr` command the message names, or `rb pnr` with no run name. |
| `block power: ...` | A block's top-layer supply pins miss the parent's straps. See [Harden a block](#harden-a-block). |
| `port 'd' is connected to N bit(s)`, `instance ... sets <P>=...` | The parent's ports or parameters differ from the hardened block's. See [Instance a parameterised block](#instance-a-parameterised-block). |
| Run refused: earlier dispatched jobs still queued | Wait, or run the printed `scancel`. If `squeue` cannot answer, remove the named `dispatch/` directory once the jobs have ended. |

## Configure the physical platform

A run picks its technology with `platform:`. A `cfg-pdks` entry supplies files and cell names; a `cfg-pnr-platforms` entry selects a PDK, its corners and per-platform settings. P&R reads these PDK keys beyond synthesis's:

| PDK key | Use |
| --- | --- |
| `tie-hi`, `tie-lo`, `fill-cells`, `pin-layers` | Tie cells, fillers, horizontal and vertical pin layers. `pin-layers` is required: `place_pins` has no default. |
| `cell-gds`, `klayout-tech`, `klayout-props` | Cell layouts, KLayout technology and colours, for stream-out. |
| `pdn-config` | Tcl declaring the power grid, without a `pdngen` call. Unset means no grid. |
| `rcx-rules` | OpenRCX rules, enabling extraction, `<top>.routed.spef` and post-route repair. `LayerCount` must match the LEF. |
| `layer-rc-tcl` | `set_layer_rc` and `set_wire_rc` lines, sourced after `read_sdc`. See [Set wire RC](#set-wire-rc). |
| `platform-tcl` | Sourced before the first Liberty read, for lines such as `suppress_message`. |
| `tracks-tcl` | Replaces the bare `make_tracks` after floorplan initialisation. |
| `tapcell-tcl` | Inserts taps and endcaps after macro placement, before the grid. Unset runs `cut_rows`, cutting rows 2 µm around each macro. |
| `dont-use-cells` | Cells no step may choose; synthesis reads it too. |
| `placement.*` | Placement defaults; platforms and runs override them. |

The four Tcl keys match ORFS's `PLATFORM_TCL`, `SET_RC_TCL`, `MAKE_TRACKS` and `TAPCELL_TCL`. They are sourced as written, so a file copied from ORFS must not read ORFS environment variables. A missing file fails setup.

`dont-use-cells` takes one name or glob per entry, with `*` and `?` only. A platform's list adds to its PDK's, for its own flow only. After routing, each instance of an excluded cell prints `RB-DONT-USE-VIOLATION: <instance> <master> <pattern>` and fails the run.

## PDK setup notes

- Nangate45 has no `pdn-config`, so its runs have no power grid. Set `pin-layers`, for example `metal3` and `metal2`. For extraction, use ORFS's `flow/platforms/nangate45/rcx_patterns.rules`.
- The template's `sky130hd` entry is a worked example: `pin-layers` `met3` and `met2`, `met*` routing layers, a `pdn-config`, `placement.density: 0.60`, a `dont-use-cells` list for the probe and `lpflow` cells, a `cts-buffer` list, and ORFS's `sky130hs/rcx_patterns.rules`. On sky130hd, set `port-buffer` for hardened blocks.
- For ASAP7:
  - Set the four Tcl keys to ORFS's `liberty_suppressions.tcl`, `setRC.tcl`, `openRoad/make_tracks.tcl`, and a copy of `openRoad/tapcell.tcl` with `TAP_CELL_NAME` and `MACRO_ROWS_HALO_X` / `_Y` filled in.
  - List each corner's split Liberty files under that corner.
  - List each threshold-voltage flavour's cell LEF (`asap7sc7p5t_28_R_1x_*.lef`, `..._L_...`) in `macro-lef`, a list read in order, and macro LEFs in `lef-paths`. In a Tcl hook, `$MACRO_LEF` names only the first.
  - Give `fill-cells` each flavour's fillers, or fill insertion fails with `DPL-0050`.
  - ORFS uses `routing-layer-adjustment: 0.25`.

## Set wire RC

Set the PDK's `layer-rc-tcl` so placement, CTS and repair see real wire delay. Without it they see the technology LEF's layer RC, which is zero on most open PDKs, and the routed design can fail hold that repair never saw.

Setup repair cannot run on zero wire RC: OpenROAD stops with `RSZ-0089`, and the description names the cause and both fixes. Three steps repair setup by default, as in ORFS, and without wire RC all three must be off:

- `floorplan-setup-repair: false`
- `post-cts-setup-repair: false`
- `global-route-repair` without `setup-margin`, such as `{hold-margin: 0}`, or `null`

rb warns `pnr.no_wire_rc` and qualifies the `PASS` when the PDK sets no `layer-rc-tcl`, or `pnr.log` has `EST-0018` (zero wire capacitance) or `CTS-0104` (zero clock wire RC). `session` hooks may set the wire RC instead. Each session then checks after the hooks that every corner has signal and clock wire capacitance, and the run is not qualified when every check passes.

For sky130hd, copy the `set_layer_rc` lines of OpenROAD's `test/sky130hd/sky130hd.rc` and pick the wire layers:

```tcl
set_layer_rc -layer met1 -capacitance 1.72375E-04 -resistance 8.929e-04
# ... one line per layer and via
set_wire_rc -signal -layer met2
set_wire_rc -clock -layer met5
```

The default [global-route repair](#repair-timing-after-routing) repairs again on the global-route estimate, which sees wire length that placement estimates miss.

## Sign off at several corners

Give a platform `corners: [tt, ss, ff]` instead of `corner:` to time and repair every corner in one OpenROAD session, so repair and the final reports see them all.

- CTS uses the first corner, the primary. With neither key, the PDK's first corner is used.
- `rb power` reads the list too, in one SDC mode ([Known Issues](../known-issues.md#rb-power-times-every-corner-in-one-sdc-mode)). `rb synth` stays single-corner.
- A multi-corner platform cannot harden a block or consume hardened blocks.

Each corner is timed in an OpenSTA SDC mode and scene of its own, named after it, so a generated clock's latency comes from the corner being timed.

- The SDC, `max-fanout` and the propagated clocks after CTS apply in every mode. The final reports read the routed SPEF once per corner.
- `constraints:` can map every corner to its own SDC: `{tt: top_tt.sdc, ss: top_ss.sdc, ff: top_ff.sdc}`. Define the same clocks in each, since CTS builds the trees from the primary's.
- CTS, hooks and an SDC command without a mode use the primary's mode. To constrain every corner from a hook, run the command after `set_mode <corner>` for each, then `set_mode <primary>`.
- `<top>.routed.sdc` holds the primary's constraints, `<top>.<corner>.routed.sdc` each corner's. A checkpoint's `.sdc` holds every mode, and `<NN>_<stage>.<corner>.sdc` one corner's as plain SDC.
- `max_fanout_violation_count` counts a pin once per corner it violates at; without modes, once.

Modes need OpenROAD 26Q4 or newer, which rb checks at setup ([Known Issues](../known-issues.md#corners-share-one-sdc-mode-on-an-openroad-without-modes)). An older one times every corner in one mode and warns `pnr.corner_modes_unsupported`, and a per-corner `constraints:` fails setup. An OpenROAD that gives no answer twice fails setup with `pnr.corner_modes_probe_failed`.

A `lib-paths` entry that is one path is timed at every corner, so a macro keeps that timing everywhere and can fail a slow corner (`RSZ-0090`). Map each corner to the macro's Liberty for it instead:

```yaml
    lib-paths:
      - {tt: macros/sram_tt.lib, ss: macros/sram_ss.lib, ff: macros/sram_ff.lib}
```

- Each file is timed at its corner only.
- A map names every platform corner and no other, or setup fails with `pnr.lib_paths_corners`. On a single-corner platform it names that one.
- A file both plain and in a map fails setup with `pnr.lib_paths_duplicate`, because OpenSTA times a cell with the last copy it read. Drop the plain entry.
- The Liberty time-unit check reads every file. `--resume` refuses a checkpoint made with another map.
- `rb power` reads a map from the P&R run or its own `lib-paths`, and accepts a platform with fewer corners than the map; see [Power](power.md).

## Gate signoff

A `signoff:` block on the platform or run makes the verdict fail a routed run on timing, DRCs or electrical violators, at the corners you name:

```yaml
    signoff:
      setup-corners: [ss]                  # default: every corner
      hold-corners: [ff]                   # default: every corner
      fail-on: [timing, drc, electrical]   # default: none
```

| Gate | Fails when |
| --- | --- |
| `timing` | Setup WNS over `setup-corners` or hold WNS over `hold-corners` is negative or not reported. Reports print two digits: `-0.00` fails, `0.00` passes. Setup with no constrained path at every one of its corners fails, since nothing was checked; hold then passes. |
| `drc` | `drc_count` is above 0, or detailed route wrote no DRC report (`drc.count` null). A clean route writes an empty report. |
| `electrical` | Any max-slew, max-capacitance or max-fanout violator, or one of the three counts is not reported. `fail-on-electrical: true` adds this gate, with or without the block. |

- A run's block overrides the platform's field by field; `null` sets a key to its default or turns the block off.
- A corner the platform does not analyse fails the platform's load, or the run's setup.
- A `--stop-after` or probe run reports its stage's values and gates nothing.
- The `signoff` field gives each gate a `verdict` (`pass`, `fail`, `not_gated`) and its values: `timing.setup` and `timing.hold` with `corners`, `wns_ps`, `worst_corner` and `unconstrained`; `drc.count`; the three violator counts. `wns_setup_ps` and `wns_hold_ps` stay the worst over every corner.
- A failed gate fails the run without `fail_stage`, so `xfail:` can excuse it, and the routed outputs stay. The description names each gate, such as `signoff failed: timing (setup WNS -0.1200 ns at ss); drc (3 violation(s))`.
- With `fail-on-electrical: true`, the electrical part keeps its own description, `electrical violators in the routed design: ... (fail-on-electrical; see electrical.rpt)`, after the `signoff failed:` prefix.

On a multi-corner platform, [post-route repair rounds](#repair-timing-after-routing) judge setup only at `setup-corners` and hold only at `hold-corners`. Without the block they judge every corner, and the result has no `signoff` field.

## Tune placement, clock tree and repair

Process-dependent settings live on the PDK and can be overridden on the platform, most also on a run, so variants of one block need no platform each. The [YAML reference](../reference/yaml.md#synthesis-and-physical-design-tools) gives each key's levels and default. A default is OpenROAD's or ORFS's, except for rb steps without a counterpart (such as `placement.macro-pin-limits`) and [known issue](../known-issues.md) workarounds.

| Problem | Setting |
| --- | --- |
| Global route overflows | Lower `placement.density` (default 0.7) or `floorplan.utilization`. Try `placement.routability-driven: true`, plus `routability-use-grt: true` for a slower, closer estimate. A fixed `placement.reference-hpwl`, such as `446000000`, spreads a large design further. |
| DRCs left in congested areas | `routing-layer-adjustment` (0 to 1) withholds that fraction of each signal layer from the global router; [`global-route.layer-adjustment`](#tune-global-routing) sets it per layer. |
| Setup misses on long nets | `placement.timing-driven: true`, which needs wire RC. On OpenROAD 26Q4 it also buffers and resizes during placement, growing area. |
| Timing misses after routing | [`global-route-repair` or `post-route-repair`](#repair-timing-after-routing). |
| Long nets that routing pushes over their slew limit | `placement.repair-design: {max-wire-length: <µm>}`, below. |
| Many fanout buffers on a library without a fanout limit (sky130hd) | `max-fanout: 100000` stops buffering for fanout alone; `repair_design` otherwise uses 50. |
| Cells too tight | `placement.padding` (sites). |
| Clock tree | `cts-buffer`, `cts-sink-clustering`, `cts-obstruction-aware`, `cts-apply-ndr`. `cts-clock-nets` builds trees for the named clocks only. |
| A strong root buffer with small branch buffers | `cts-root-buffer` names the root, and `cts-buffer` lists the branch buffers only. Otherwise CTS picks the root from `cts-buffer`. |
| Slew violations on clock pins far from the nearest leaf buffer, as when many clock gates sit in a small area | `cts-sink-clustering` with `size` and `max-diameter` (µm). |
| Setup misses on flops of a clock branch CTS padded to match the deepest (`CTS-0033`, `CTS-0036`), as when a clock drives a buffer as well as flops | `cts-latency-balancing: false`. |
| Long clock wires after CTS | `cts-repair-clock-nets: true`, or `{max-wire-length: <µm>}`. |
| A block must leave the top layers to its parent | `routing-layers` on the run. |
| A block needs other tap spacing or endcaps | `tapcell-tcl` on the platform or run. |
| Cells crowd macro edges, or Metal Spacing DRCs at a block edge | `placement.macro-cell-halo` (µm), or a `tapcell-tcl` that cuts rows around macros. |
| Tie cells too close to their loads | `placement.tie-separation` (µm). |
| Macro pins over a tight Liberty limit, such as OpenRAM SKY130 SRAM inputs (0.04 ns) | `placement.macro-pin-limits`, below. |
| Legalization after routing repair is slow, or fails with `DPL-0701` then `DPL-0033` | `placement.legalizer`, below. |

A platform that ends up with `routability-use-grt` but not `routability-driven` fails to load.

Each CTS setting sits on the platform and the run, and a run's value replaces the platform's whole:

```yaml
    cts-buffer: [sky130_fd_sc_hd__clkbuf_4]
    cts-root-buffer: sky130_fd_sc_hd__clkbuf_16
    cts-sink-clustering: {size: 20, max-diameter: 25}
    cts-obstruction-aware: false
    cts-repair-clock-nets: {max-wire-length: 1000}
```

This calls `clock_tree_synthesis -root_buf sky130_fd_sc_hd__clkbuf_16 -buf_list sky130_fd_sc_hd__clkbuf_4 -sink_clustering_enable -sink_clustering_size 20 -sink_clustering_max_diameter 25 -no_obstruction_aware`, then `repair_clock_nets -max_wire_length 1000`.

### Threshold-voltage flavours and footprints

On a library with several threshold-voltage flavours, such as ASAP7 RVT and LVT with both flavours' Liberty per corner and both LEFs in `macro-lef`, resizing moves cells between flavours freely.

- `keep-sizing-vt: true` keeps a cell's flavour when `repair_design` or `repair_timing` resizes it; only timing repair's threshold-voltage swap changes it.
- `dont-use-cells` keeps a flavour out of every step.
- `cts-dont-use-cells` keeps cells out of the clock tree only: CTS and `repair_clock_nets` skip them, and post-CTS repair may still use them on data paths.
- It governs only cells CTS picks itself, dummy-load inverters and inferred delay buffers. Neither CTS nor `repair_clock_nets` checks `cts-buffer` or `cts-root-buffer` against either list, so name the flavour there.

`match-cell-footprint: true` on the PDK or platform makes resizing keep each cell's footprint, as ORFS does for platforms that set `MATCH_CELL_FOOTPRINT` (sky130hd and ihp-sg13g2 among them). It adds `-match_cell_footprint` to every `repair_design` and `repair_timing` the flow emits, the post-route and electrical rounds included, but not those a hook runs; pass the flag there yourself. rb runs no `repair_timing -recover_power`, which ORFS also gives the flag.

### Bound wire length during placement

`placement.repair-design` passes `slew-margin`, `cap-margin` and `max-wire-length` to the place stage's `repair_design`, as `global-route-repair.repair-design` does after global route. Unset, it uses OpenROAD's defaults, as ORFS does.

```yaml
    placement:
      repair-design: {max-wire-length: 600, slew-margin: 10}
```

`repair_design` judges wires on the placement estimate, which a long routed net can exceed. `max-wire-length` splits any longer wire with a buffer, so one driver does not carry a long chain to routing, where post-route repair may not see it ([Known Issues](../known-issues.md#post-route-repair_design-sizes-its-buffering-on-the-placement-estimate)).

### Buffer macro pins with tight limits

`repair_design` sizes repeaters for the library's target slew, so a macro pin with a much lower limit stays in `electrical.rpt`. `placement.macro-pin-limits` buffers such pins at the end of the place stage, after `repair_design`, the `after-repair-design` hook and legalization:

```yaml
    placement:
      macro-pin-limits:
        buffers: [sky130_fd_sc_hd__buf_1, sky130_fd_sc_hd__buf_2, sky130_fd_sc_hd__buf_4,
                  sky130_fd_sc_hd__buf_8, sky130_fd_sc_hd__buf_12, sky130_fd_sc_hd__buf_16]
        margin: 0.05              # of each max-slew limit, kept in hand
        capacitance-margin: 0.5   # of each max-capacitance limit; default: margin
```

- Each macro data pin over its limit less the margin gets a buffer fixed on the nearest free row site; an input also gets a pre-driver. Buffers are sized from `buffers` weakest first and marked `dont_touch`, so later repair leaves them alone.
- When no pair meets the limit less the margin, the closest pair stays (for an input, the lowest slew), which is not always the strongest, and the pin counts in `macro_pin_unmet_count`.
- `pnr.log` has one `>>>` line per pin.

The step judges on placement parasitics, so check `electrical.rpt`:

- A long macro-output net can route to twice its estimated capacitance, hence `capacitance-margin: 0.5` on sky130hd.
- A large `macro-cell-halo` keeps the buffer too far from the pin: on sky130hd, 3 µm works with `buf_8` and 20 µm does not.
- A pin route that global routing detours around the macro is out of reach.

### Choose the legalizer per stage

OpenROAD 26Q4 legalizes with one of two engines:

- Negotiation, the default. Cells compete for sites over many iterations. It adds less wire length where a step crowds many cells in, such as hold repair after CTS.
- Diamond search, `-use_diamond_legalizer`. Each cell takes a nearby free site. It is much faster, and as good where a step adds few cells, such as the repairs after global and detailed route. On a crowded design negotiation can stop at its iteration cap (`DPL-0701`) with cells still overlapping, which fails `check_placement` (`DPL-0033`).

`placement.legalizer` picks the engine per stage. This keeps negotiation through CTS and uses diamond search from global route on:

```yaml
    placement:
      legalizer: {global_route: diamond, post_route_repair: diamond}
```

- `legalizer: diamond` sets every stage. A stage the mapping leaves out passes no option, so OpenROAD's default applies.
- It applies to every `detailed_placement` rb runs in the stage, `placement.macro-pin-limits` included. A hook passes its own options.
- With `rotated-macro-footprint: diamond-legalizer`, a turned macro makes every stage use diamond search, including one set to `negotiation`.
- Diamond search stays on for the rest of an OpenROAD session ([Known Issues](../known-issues.md#diamond-search-stays-on-for-the-rest-of-an-openroad-session)). A one-session run that sets a stage to `negotiation` after one set to `diamond` fails at setup; run it with [`stages: true`](#run-stages-as-separate-sessions). Each post-route round is a session of its own.
- Each stage's engine is in that stage's script only. A [resume](#run-stages-as-separate-sessions) that changes it for stages from `--from` on needs no `--accept-change`.
- An OpenROAD whose `detailed_placement` has no `-use_diamond_legalizer` fails the run at setup.

The log shows each call's engine on its `legalizer` line, under `Detailed Placement Analysis`.

### Optimisation steps

The flow runs ORFS's default optimisation steps. Each key below turns one off, on the platform or the run, at the cost in the last column:

| Step | Where | Key | Gives up |
| --- | --- | --- | --- |
| `repair_timing -setup`, buffer removal and sizing only | End of floorplan | `floorplan-setup-repair` | Setup slack |
| `improve_placement`, then `optimize_mirroring` | After place legalization | `placement.improve-placement`, `placement.optimize-mirroring` | Shorter wires |
| `repair_clock_inverters` | Before CTS | `cts-repair-clock-inverters` | Keeping CTS from buffering inverted clocks |
| `repair_design`, setup and hold `repair_timing` | After global route | `global-route-repair: null` ([details](#repair-timing-after-routing)) | Setup slack |
| `repair_antennas` (5 iterations), `check_antennas` into `grt_antennas.rpt` | After global-route repair and its hooks | `global-route-antenna-repair` | Antenna repair, which no other step does |
| `repair_antennas` and detailed route while violations remain (at most 5), `check_antennas` into `drt_antennas.rpt` | After detailed route and each post-route round's | `detailed-route-antenna-repair` | Antenna repair |

Antenna repair inserts the PDK's antenna diodes, LEF class `CORE ANTENNACELL`. A [probe](#probe-a-floorplan) skips the floorplan setup repair, the global-route repair and the antenna repair after global route.

## Check and repair slew at output ports

By default slow output pins pass every check. A port has no max-slew or max-capacitance limit unless the SDC sets one, so the electrical counts and gate pass a long net to an output pin however slow it is. `repair_design` does not repair a port's slew even with an SDC limit, since it checks the net at its driver only.

To check and fix outputs, do both:

1. In the SDC, set `set_max_transition` on the outputs at the library's limit (for example `set_max_transition 320 [all_outputs]`) and `set_load` for what they drive. The violator counts and `electrical.rpt` then include output pins.
2. Buffer the outputs and hold the buffers at their pins:

```yaml
    buffer-ports:
      inputs: false                # default true
      skip-clock-outputs: true     # default false
      hold-outputs-at-pins: true   # default false
```

Each output net then ends at a buffer input with a Liberty limit, which placement's `repair_design` meets by resizing the driver or adding repeaters.

- Without `hold-outputs-at-pins`, global placement may put the buffer anywhere between driver and pin, and the buffer-to-pin net again ends at a port.
- With it, after pin placement each buffer goes on the row site nearest its pin that no other held buffer uses, fixed (`FIRM`) through global placement.
- It is freed right after, before `repair_tie_fanout` and the `after-global-placement` hooks, so `repair_design` repairs the driver net and legalization places the buffer.
- That repair uses placement parasitics, so a long driver net can still route over its limit. `electrical.rpt` lists it as a max-slew violator on the buffer's input.

`buffer_ports` also buffers outputs that carry a clock: a forwarded clock on the clock network, or an output with a generated clock. `skip-clock-outputs: true` leaves them unbuffered and names each in `pnr.log`.

`buffer-ports: true`, the default of a `harden: true` run, buffers every input and output as OpenROAD and ORFS do, without the two options. `inputs` and `outputs` map to `buffer_ports -inputs` and `-outputs` ([YAML Formats](../reference/yaml.md#pnryaml)).

## Repair timing after CTS

After CTS the flow repairs setup (unless `post-cts-setup-repair: false`), then hold, on placement parasitics. `post-cts-repair` sets their margins in ns; the [YAML reference](../reference/yaml.md#synthesis-and-physical-design-tools) says how each is passed.

```yaml
    post-cts-repair:
      setup-margin: 0.05
      hold-margin: 0.01
```

- The setup margin also goes to the hold repair, which then keeps that much setup slack, on placement parasitics, on the paths it delays.
- Set it when post-CTS hold buffers leave setup-critical paths failing after routing, since later repairs do not remove them.

`max-buffer-percent`, in `post-cts-repair`, `global-route-repair` or `post-route-repair`, goes to that block's `repair_timing -hold` as `-max_buffer_percent`. It caps the buffers hold repair inserts, as a percentage of the design's instances. Unset keeps OpenROAD's default of 20%, as ORFS does. Raise it when hold repair stops with `RSZ-0060` (max buffer count reached), such as on a block whose input registers need padding on most bits.

## Repair timing after routing

Post-CTS repair times the design on placement estimates. Two blocks, on the platform or run, repair again on routed parasitics. Both cost run time and change QoR.

| Block | Runs | Default |
| --- | --- | --- |
| `global-route-repair` | After global route, on its estimate. Then legalizes and reroutes the changed nets (`reroute: incremental`, default) or the whole design (`full`). | On, as ORFS: `{repair-design: {}, setup-margin: 0, hold-margin: 0}` when neither platform nor run writes it or `global-route-hold-repair`. |
| `post-route-repair` | The stage `post_route_repair`, after detailed route, on extracted parasitics. Needs `rcx-rules`. | Off. |

```yaml
cfg-pnr-platforms:
  - name: sky130hd_tt
    # ...
    global-route-repair:
      repair-design: {slew-margin: 20, cap-margin: 20}
      setup-margin: 0.05      # ns
      hold-margin: 0.02       # ns
    post-route-repair:
      setup-margin: 0.05
      rounds: 3
      margin-step: 0.02
```

A block runs `repair_design` when `repair-design` is set (`{}` for OpenROAD's defaults), and setup or hold `repair_timing` when that margin is set. A written block runs only what it sets; the [YAML reference](../reference/yaml.md#synthesis-and-physical-design-tools) gives the rest of the merge rules.

### Post-route repair rounds

`post-route-repair` extracts and times the detailed route as round 0, then runs up to `rounds` (default 1) repair rounds, each in fresh sessions. Each round:

1. Repairs on the extracted parasitics, with the margins plus (k - 1) × `margin-step` in round k. `rounds` above 1 needs a margin.
2. Routes again. `reroute: eco` (default) keeps the detailed route and reroutes the changed nets; `full` reroutes everything, costing about a global and a detailed route.
3. Repairs antennas as after detailed route, with `detailed-route-antenna-repair`.
4. Extracts and times.

An `eco` round repairs hold in two calls:

1. Without a hold margin, estimating the nets it changes with no signal wire RC. Each endpoint that fails on the extracted parasitics gets delay on its path.
2. With `hold-margin`, at the round's signal wire RC. A block with `hold-margin: 0` runs the first call alone.

- `repair_timing -hold` tries a buffer at each driver along a failing path and rolls it back when it costs too much slew or setup slack. The net it tried then carries an estimate in place of its extracted parasitics.
- Where that estimate is slower than the route, the endpoint reads as met with no buffer on its path, and fails again after extraction, by the same amount in every round. With no wire RC the estimate is never slower than the route.
- A net the first call buffers loses its wire delay in the estimate, so the call can add more buffers than the path needs. It is limited to the endpoints that fail outright for that reason.
- The second call can still read a rolled-back net as slower than it routes, so the round can end with hold met by less than `hold-margin`.
- Each call takes `max-buffer-percent`.
- A `full` round estimates changed nets from their global routes and repairs hold in one call.

Each round, the electrical round included, starts with `remove_fillers`, which removes every `CORE_SPACER` instance that is not `LOCKED`. A spacer a hook placed `FIRM` or `FIXED` to reserve sites is gone after the first round; place it `LOCKED`.

Three keys apply to `post-route-repair` only, all off by default:

```yaml
post-route-repair:
  repair-design:
    slew-margin: 20
    wire-rc: {resistance: 0.06, capacitance: 0.17}   # kohm/um, fF/um; or {layer: M5}
    after-timing: true
  timing-wire-rc: {resistance: 0.03, capacitance: 0.16}
  setup-margin: 0.05
```

- `wire-rc` sets the signal wire RC at which each round's repairs (`repair_design`, `repair_timing`, `after-timing-repair` hooks) estimate the nets they change, except an `eco` round's first hold call. The session's RC is set for placement, but routed nets are often longer and higher in the stack: at that RC `repair_design` can leave them over their slew limit, and `repair_timing` can judge that removing a repeater from a long wire saves time.
- `{layer: M5}` takes that layer's RC per corner from `set_layer_rc`, else the LEF. Clock wire RC is unchanged, and each repair line in `pnr.log` names the signal wire RC it used.
- The session's signal wire RC, including one a `session` hook set, returns after the round's last repair. Changed nets keep the `wire-rc` estimate until OpenROAD estimates them again, such as after legalization moves their cells.
- `timing-wire-rc` gives `repair_timing` and the `after-timing-repair` hooks an RC of their own, in either form `wire-rc` takes; `repair_design` keeps `wire-rc`, or the session's RC without one. Set it when `wire-rc` is above the routed mean so that `repair_design` buffers the nets that route worst: at that RC the timing repair over-estimates the nets it changes. It needs a margin.
- `wire-rc` and `timing-wire-rc` fail setup with `pnr.wire_rc_unsupported` on an OpenROAD older than the one the [YAML reference](../reference/yaml.md#synthesis-and-physical-design-tools) names for them.
- `after-timing: true` runs `repair_design` again after setup and hold repair, before legalizing, since `repair_timing` checks slew at the session's RC and can leave nets a few picoseconds over. It needs a margin.

### Which round is kept

The stage keeps the best round, never worse than the detailed route. A repaired side is setup when `setup-margin` is set and hold when `hold-margin` is set; a met side's worst slack counts as 0. The first rule that applies decides:

1. A clean route beats one with DRCs.
2. With no more DRCs, a round wins when no repaired side's worst slack is lower and one is higher.
3. A round that ties on timing wins with fewer DRCs.
4. A round better on one side and worse on the other, with no more DRCs, wins when its total negative slack summed over the repaired sides is smaller. So a round that closes hold and cuts setup TNS wins even with one setup path a few picoseconds worse, and the next round starts from it.
5. When that TNS ties or is not reported, the smaller worst violation (lowest worst slack over the repaired sides) wins. On equal worst violations the best stays.
6. When the rounds count electrical violators, a round tying on every side's worst slack and on DRCs wins with fewer max-slew plus max-capacitance violators. The stop reason names this tie-break.

The rounds count electrical violators when `electrical-repair` is set or the run gates on them (`fail-on-electrical: true`, or `electrical` in `signoff.fail-on`). A gating run's stop reason also names the kept round's violators.

With [`signoff:`](#gate-signoff) on a multi-corner platform, each side's worst and total negative slack, here and in the stop rules, come from its signoff corners only; a side's TNS is its worst signoff corner's. Each `pnr.post_route_round` event adds them as `signoff_timing`, and `pnr.post_route_repair_done` lists `judged_corners`.

### When the rounds stop

| `post_route_repair.stop` | When |
| --- | --- |
| `timing_met` | The best round is clean and meets timing. |
| `no_improvement` | A clean round does not improve on the best. |
| `timing_not_reported` | Timing is not reported. |
| `session_failed` | A session fails. |
| `last_round` | `rounds` ran out. |

- `timing_met` and `no_improvement` need a clean route, so rounds with DRCs continue.
- The first clean round that does not become the best ends the stage, so raising `rounds` does not change that.
- Rounds that count electrical violators also continue while the best round meets timing but has max-slew or max-capacitance violators and the block sets `repair-design`, and after a clean round that is not the best but has fewer such violators.
- A failure extracting round 0 fails the run. A later round's failed session warns `pnr.post_route_round_failed`, keeps the best round and qualifies the `PASS`.
- `finish` continues from the kept round, whose DRC report becomes `route.drc.rpt`.
- `pnr.post_route_round` logs each round's slack and DRCs, and in `decided_by` the comparison that decided it: `drc_violations`, `timing`, `total_negative_slack`, `worst_violation`, `electrical_violators`, or `tie` when the best stays.
- When a trade of sides made the kept round the best, or a clean trading round stopped the rounds, the stop reason gives both rounds' TNS and worst violations.

### Repair electrical violators after routing

A round's reroute can leave nets over their max-slew or max-capacitance limit. `electrical-repair` adds one round after the others to repair them:

```yaml
post-route-repair:
  repair-design: {slew-margin: 20, cap-margin: 20}
  setup-margin: 0.05
  electrical-repair: {slew-margin: 10, cap-margin: 10}   # percent; {} for OpenROAD's defaults
```

When the best round has such violators, the electrical round:

1. Runs `repair_design` at these margins on that round's database and SPEF, at `repair-design.wire-rc` when set.
2. When the block sets a margin, runs setup and hold repair at the block's margins (no `margin-step`) with the `after-timing-repair` hooks, then `repair_design` again, so the new buffers do not hand back a hold failure.
3. Legalizes, reroutes per `reroute`, extracts and times like any round.
4. Is kept only if it [beats the best](#which-round-is-kept).

Otherwise it is skipped, and `pnr.log` says so. Its number is `rounds` + 1. `post_route_repair.electrical_round` gives `source_round`, `status` (`kept`, `not_kept`, `skipped`, `failed`) and `reason`, and a failed session is set aside like any round's.

`repair_design` finds violators on the SPEF but sizes their buffers on the placement estimate at the round's wire RC, so it leaves a net whose estimate is within the limit less the margin, however far over the extraction is ([Known Issues](../known-issues.md#post-route-repair_design-sizes-its-buffering-on-the-placement-estimate)).

- When the round is kept, rb lists each violator still on its SPEF that the estimate judges within the limit less the margin, at the scene where its extracted violation is worst. It warns `pnr.electrical_round_unseen` naming them.
- `electrical_round.unseen` gives each one's `check` (`slew` or `capacitance`), `pin`, `limit`, `extracted` and `estimated` value.
- The list leaves out ports, and pins on constant-driven, clock, `dont_touch`, special or abutment nets, which `repair_design` never repairs.
- To fix them, raise `repair-design.wire-rc` toward the extracted RC, or bound placement's wire length with `placement.repair-design.max-wire-length`.

With `electrical-repair` or an electrical gate, each `pnr.post_route_round` event carries `max_slew_violation_count` and `max_capacitance_violation_count` over every corner, and each corner's in `corners` on a multi-corner platform. Without either, rounds count no violators and their scripts are unchanged.

## Tune global routing

The `global-route` block on the platform or run sets `global_route` options; a run's block overrides the platform's field by field. The [YAML reference](../reference/yaml.md#synthesis-and-physical-design-tools) gives each key and the calls it reaches.

```yaml
global-route:
  congestion-iterations: 100
  congestion-report-iter-step: 25
  grid-origin: [3, 3]                       # DBU
  layer-adjustment: {met1: 0.9, met2: 0.8}
```

| Problem | Key |
| --- | --- |
| Overflow that more rip-up and reroute would clear | `congestion-iterations` (default 30), with `congestion-report-iter-step` to watch it fall. |
| Long wires low on the stack, or one congested layer | `layer-adjustment` per layer, applied after `routing-layer-adjustment`. |
| Boundary pins centred on a GCell edge fail detailed route (ASAP7 pins on a fine lattice) | `grid-origin`. |
| Critical nets on resistive layers | `critical-nets-percentage`, `resistance-aware`. |
| Seeing where a design overflows without failing at global route | `allow-congestion: true`. |

OpenROAD drops `grid-origin` on every call, so rb passes it to each, including the incremental routes of both repairs. OpenROAD 26Q4 offsets only the guides, not the GCell grid, so rb rebuilds the grid from the guides before each detailed route. It does so after the `global_route` checkpoint, so a staged or resumed detailed route starts from the router's own route, as a one-session run does.

`allow-congestion` is for diagnosis: global route keeps its overflow and the flow continues to detailed route. The result is a `PASS` qualified `global-route.allow-congestion (diagnosis only)` with `allow_congestion: true` and `global_route_overflow`, never a clean pass. Such a run cannot be `harden: true`.

## Probe a floorplan

`mode: probe` checks quickly whether a floorplan places and routes.

- It skips setup and hold repair (floorplan, post-CTS and global-route) and the antenna repair after global route, stops after global route, and writes the `global_route` checkpoint.
- It passes with `routed: false`, the global-route overflow, and timing on the global-route estimate.
- `xfail:` does not apply, and a `closure` run cannot resume from its checkpoints.
- It publishes no abstract, so `harden: true` with `mode: probe` fails to load.

## Shape the floorplan

The floorplan sets the die and core and keeps cells out of chosen areas.

- `floorplan.utilization` (no default), `aspect` (default 1.0) and `core-margin` (default 1.0 µm) size the die from the netlist's cell area.
- `die-area` and `core-area`, set together as `[x0, y0, x1, y1]` µm, fix it exactly and exclude those three.
- `core-cutouts` carves rectangles out of the core for an L- or T-shaped core. They block cells, rows, taps and macros, not routing; the die stays rectangular. Lower `utilization` to leave room for them.

`floorplan.blockages` keep standard cells out of rectangles (die µm) and need OpenROAD 26Q1:

```yaml
    floorplan:
      blockages:
        - rect: [10, 10, 130, 60]                 # hard: no cells, rows cut
        - rect: [400, 300, 520, 420]
          type: partial                            # global placement caps density here
          max-density: 0.4
        - rect: [200, 50, 260, 120]
          type: soft                               # global placement only; repair may use it
          lifetime: global-placement               # removed after global placement
```

- `lifetime: global-placement` makes any type a screen for global placement alone: no rows are cut, macro placement ignores it, and no checkpoint holds it.
- A macro overlapping a `hard` blockage or core cut-out fails the run, naming the macro, the region and the fixes: a `floorplan.macros` location, a larger `placement.macro-channel`, or moving the blockage. rb checks after macro placement, and again at the end of the floorplan when an `after-pdn`, `after-pins` or `stage-end` hook is set.
- RTL-MP treats every blockage as an obstacle and stops with `MPL-0065` or `MPL-0040` when it finds no clear placement. rb checks only `hard` blockages and cut-outs.

## Place macros

Hard macros (SRAMs, hardened blocks) are placed before the power grid by OpenROAD's RTL-MP (`rtl_macro_placer`), as in ORFS. It needs OpenROAD 26Q4 or newer; on an older one a run with macros stops at macro placement saying so.

- RTL-MP places by connectivity and may mirror a macro. It reports to `rtlmp/`, and `pnr.log` has one `placed` line per macro.
- rb passes ORFS's arguments: `placement.density` as `-target_util`, and `placement.macro-channel` as `-min_channel_size`, the minimum channel in µm between macros and from the core edge. RTL-MP trims each channel to the pins along it unless `placement.macro-pin-aware-channels: false`.
- ORFS sets a channel on every platform, such as `[80, 80]` on sky130hd. rb sets none unless configured, and RTL-MP's own channels can leave none. Set one when the grid fails with `PDN-0179` or global routing with `GRT-0116`.
- To place macros yourself, locate them with `floorplan.macros` or place them in a [`macro-placement` hook](#run-tcl-hooks), which replaces RTL-MP. Either way, no macro may overlap a core cut-out or [`hard` blockage](#shape-the-floorplan).

`floorplan.macros` fixes, orients or haloes individual macros:

```yaml
    floorplan:
      macros:
        - instance: u_core/u_sram          # instance name or glob (* crosses /)
          location: [200, 290]             # lower-left corner after orientation, die µm
          orientation: MX
          halo: [3, 3]                     # replaces macro-cell-halo for this macro
        - instance: "*/u_rf*"
          halo: [2, 2]                     # every macro the glob matches
        - instance: "g_bank[*].u_ram"      # generate loop: brackets are literal
          halo: [2, 2]
```

- `instance` is the hierarchical instance name as the netlist spells it, such as `g_bank[0].u_ram` for a macro in a generate loop. Brackets are literal, and `*` and `?` are the only wildcards. OpenROAD stores that name as `g_bank\[0\].u_ram`, and that spelling also works.
- A pattern that matches nothing this way is tried as a Tcl glob on the stored name, where `[01]` is a character class, so plans written for earlier releases keep working.
- A `location` is snapped to the site and row grid, and must be inside the core, clear of other fixed macros, and match one macro. RTL-MP places the rest around it, and does not run when every macro is located.
- An `orientation` needs a `location`, since RTL-MP orients the macros it places, unless a `macro-placement` hook places them.
- A pattern that matches nothing fails the run.
- A turned macro (`R90`, `R270`, `MXR90`, `MYR90`) turns its pins and power pins. Give the `pdn-config` a macro grid for that orientation, or `pdngen` fails with `PDN-0233`. `placement.rotated-macro-footprint` works around OpenROAD's legalizer for turned macros ([Known Issues](../known-issues.md#openroads-legalizer-blocks-a-rotated-macros-unrotated-footprint)).
- `floorplan.macros-file` takes a floorplanner's YAML or JSON list of the same entries. `macro-snap: false` keeps exact coordinates.

## Place IO pins

`floorplan.pins` places IO pins by port name or glob:

```yaml
    floorplan:
      pins:
        - {names: [req_*, clk], side: left}
        - {names: rsp_*, side: right, start: 10, end: 80}   # die µm along the edge
        - {names: pwdata, side: top, group: true, order: true}
        - {names: irq, location: [0, 42.5], layer: met3, size: [0.3, 0.6]}
```

- A `location` places one exact pin, moved onto the nearest die edge and routing track; `pin-snap: false` keeps its centre.
- Exact pins go first, and side and group rules skip them. When every signal port has an exact pin, `place_pins` is skipped.
- A name that matches no port fails the run.
- `pins-file` takes a planner's YAML or JSON list of exact pins and `floorplan.pins` entries.
- `pins-complete: true` fails the run (`fail_stage: pins_complete`) when a signal port has no entry, more than one exact pin, or no exact pin and several side or group rules.
- After routing, located macros and exact pins are compared with the routed DEF; a moved one fails with `fail_stage: routed_check`.

For other pin Tcl, such as `-mirrored_pins` or `exclude_io_pin_region`, set `pin-constraints: pins.tcl` on the run; it is sourced just before pin placement. Do not constrain a port with an exact pin there (`PPL-0107`). Pin commands do not belong in the SDC, which is read before the die exists.

## Plan the power grid

The PDK's `pdn-config` declares the grid for every run. A run can replace it with `pdn-config:` (another Tcl file), or replace its core grid with `pdn:` in YAML. `pdn:` keeps the `pdn-config`'s global connections, voltage domains and macro grids, so it still needs one.

```yaml
    pdn:
      ring: {layers: [met5, met4], width: 1.6, spacing: 1.7, offset: 2}
      stripes:
        - {layer: met1, width: 0.48, followpins: true}
        - {layer: met4, width: 1.6, pitch: 20, offset: 5}
        - {layer: met5, width: 1.6, pitch: 20, offset: 5}
```

- Each stripe layer connects to the next unless `connect: [[met1, met4], ...]` lists the pairs.
- A ring must fit between core and die. Only one always-on power domain is supported.
- The PDK's global connections are applied again at the end of every stage, so cells added after `pdngen` get supply connections.

A macro grid with a `-halo` needs the rows cut at least that far around each macro, or `pdngen` fails with `PDN-0008` where a halo reaches a row the macro does not sit on. The tap-cell step cuts them after macro placement. Without a `tapcell-tcl` it runs `cut_rows` with 2 µm, as ORFS does, which suits ORFS's 2 µm halos.

For a wider halo, cut the rows in the PDK's `tapcell-tcl`, for example `cut_rows -halo_width_x 5 -halo_width_y 5` before `tapcell`. Shrinking the halo instead can leave core straps ending next to the macro, blocking its pin escapes.

## Run Tcl hooks

`hooks:` on a run sources your Tcl at fixed points of the flow, for steps rb does not have. Each point takes a path or list, relative to `pnr.yaml`:

```yaml
    hooks:
      session: [wire_rc.tcl]
      after-cts: [io_latency.tcl]
```

| Point | Sourced |
| --- | --- |
| `session` | At the start of every session, after the SDC, before the floorplan. Also in the run's [`netlist-source: pnr` power run](power.md#extracted-parasitics). |
| `after-floorplan` | After floorplan, tracks, blockages, tie cells and the floorplan setup repair. |
| `macro-placement` | Instead of RTL-MP, only when the netlist has macros. `$rb_movable_macros` lists the unlocated ones. |
| `after-pdn` | After taps and the power grid. |
| `after-pins` | After pin placement, before port buffering. A hook that buffers ports needs `buffer-ports: false`. |
| `after-global-placement` | After `global_placement`. |
| `after-repair-design` | After `repair_design`, before legalization and `placement.macro-pin-limits`. |
| `before-cts` | After legalization, `improve_placement`, `optimize_mirroring` and `placement.macro-pin-limits`, before `repair_clock_inverters` and CTS. `cts-dont-use-cells` are not yet excluded. |
| `before-repair-clock-nets` | Right after `clock_tree_synthesis`, before `cts-repair-clock-nets`. `cts-dont-use-cells` are still excluded. |
| `after-cts` | After CTS and any `repair_clock_nets`, before setup and hold repair. |
| `after-timing-repair` | Right after the `repair_timing` calls of the post-CTS repair, the global-route repair and each post-route round, before their legalization. See below. |
| `after-cts-repair` | After post-CTS repair and its legalization. |
| `after-global-route` | After `global_route`, before global-route repair. |
| `after-global-route-repair` | After global-route repair, before antenna repair. It is in the `global_route` stage, so a probe runs it. |
| `before-detailed-route` | Before the flow's detailed route and each post-route round's, not the antenna repair's. |
| `after-extraction` | After `write_spef` in every session that writes one (each post-route round and the final extraction), before rb reads it. `$rb_spef` is its path. Needs `rcx-rules`. |
| `before-reports` | After fill and extraction, before the final reports. |
| `after-reports` | After the final reports, on their parasitics, before rb writes the routed outputs. |
| `stage-end` | At the end of every stage, before its checkpoint, with `$rb_stage` and `$rb_mode` (`closure` or `probe`). In post-route repair, at the end of each round, the electrical round included; when round 0 stops the rounds, none runs and the stage keeps the detailed route. |

`after-timing-repair` runs on the parasitics the repair used:

- `$rb_repair` is `post_cts`, `global_route` or `post_route`, and `$rb_repair_round` the post-route round's number.
- `$rb_setup_margin` and `$rb_hold_margin` are the margins passed to `repair_timing`, in the Liberty time unit, `0` for none. The hold repair also gets the setup margin.
- In an `eco` round it runs inside the incremental global route, before `repair-design.after-timing`'s `repair_design`. The electrical round runs it when the block sets a margin.
- It does not run after the floorplan setup repair, or where no `repair_timing` runs: a probe, or a repair block without a margin.

How hooks run:

- At Tcl global scope in `artefacts/<run>/`, with `$OUT_DIR` and `$DESIGN` set.
- A Tcl error, `exit` or stray `return` fails the run, naming the point and file, and for `stage-end` the stage. In a post-route round, where other failures only set the round aside, a `stage-end` or `after-timing-repair` error still fails the run.
- An `[ERROR` line OpenROAD prints while a hook runs, such as from a command the hook wraps in `catch`, fails the run only when the hook ends with a Tcl error. When the hook returns, rb warns `pnr.hook_error_lines` with the count and the first line. `pnr.log` brackets each hook's output with `>>> rb: hook begin` and `>>> rb: hook end` lines.
- Results come from the final reports; a report a hook prints does not change them.
- On a multi-corner platform an SDC command in a hook constrains only the [primary corner's mode](#sign-off-at-several-corners).
- Every later `read_spef` reads what `after-extraction` hooks left: the next round's, the final reports' and `rb power`'s. A multi-corner platform reads one SPEF per corner, so one edit covers all.
- Each point belongs to the stage it runs in (`before-cts` to `cts`): a staged run runs it in that stage's session, and `--resume` from that stage runs it again. `after-timing-repair` belongs to `cts`, so a `--resume` from a later stage is refused when its files changed.
- When a run splits into sessions (`stages: true`, `--resume`, `--dispatch slurm`, `post-route-repair`), procedures, `rename` wrappers, `set_wire_rc` and `set_dont_use` a hook set are gone in the next session. Put them in a `session` hook. rb warns `pnr.hook_commands_not_kept` when a hook redefines an existing command.

rb replaces same-named files a hook wrote in `$OUT_DIR`: `timing.rpt` and `electrical.rpt` in the final reports; after the `after-reports` hooks, `<top>.def`, `<top>.routed.v`, `<top>.routed.pwr.v`, `<top>.routed.sdc`, `<top>.<corner>.routed.sdc`, `<top>.routed.odb` and, with `harden: true`, `abstract/`; and after OpenROAD exits, `worst_paths.rpt` and `fanout.rpt`. Other names are in [Inspect artefacts](#inspect-artefacts). Every run first deletes files ending in `.def`, `.routed.v`, `.routed.pwr.v`, `.routed.sdc`, `.routed.odb`, `.routed.spef`, `.gds` or `.png`, so name a hook's files otherwise.

Hook files are inputs of checkpoints and hardened abstracts. rb does not follow `source`, so list files the SDC or a hook sources under `inputs:`, relative to `pnr.yaml`:

```yaml
    inputs: [scripts/window.tcl]
```

Each is fingerprinted by path and content with the hooks: editing one, or listing another in its place, refuses a `--resume` and makes a hardened abstract stale. `rb pnr` fails before any run starts when a run it would run lists a missing file.

## OpenROAD threads

`threads:` sets OpenROAD's thread count. Unset, rb passes none and OpenROAD uses every hardware thread of the host on 26Q4, one on older versions.

- A count, or `auto`: the CPUs of the Slurm allocation or affinity mask the run is in, 1 outside one, never the host's core count.
- A count above the allocation is clamped with `openroad.threads_capped`.
- Run inside an allocation (`srun -c 8 rb pnr ...`) or use [`--dispatch slurm`](#dispatch-the-stages-to-slurm) to give it CPUs.
- Results can differ slightly between thread counts.

The result's `openroad_threads` gives `requested` and `effective`, the count OpenROAD logged or rb set.

- With `threads:` unset OpenROAD logs no count, so `effective` is null, as is the per-stage `threads` a staged run records in `stage_end` events and the manifest.
- A one-session run records no per-stage `threads`.
- `rb power` and `rb synth`'s OpenROAD stage read `threads:` and report `openroad_threads` the same way.

## Keep stage checkpoints

A run killed at a wall-time limit leaves only its log. `checkpoints: true`, a stage or a list saves `<NN>_<stage>.odb`, `.def` and `.sdc` at stage boundaries in `checkpoints/<run-id>/`, with `checkpoints/latest` pointing at the newest, so you can inspect or resume:

| Stage | Holds |
| --- | --- |
| `floorplan` | Floorplan, macros, power grid, pins. |
| `place` | Legalized global placement that passed `check_placement`. |
| `cts` | Clock tree and hold repair. |
| `global_route` | The route after global-route and antenna repair, with guides, segments and `grt_antennas.rpt`. With `global-route.grid-origin`, before the GCell grid is rebuilt. |
| `detailed_route` | The route after antenna repair, before fill, with its DRC report and `drt_antennas.rpt`. |
| `post_route_repair` | The kept round, with its DRC report and `drt_antennas.rpt` (only with `post-route-repair`). |

- On a multi-corner platform the `.sdc` holds every mode, and `<NN>_<stage>.<corner>.sdc` one corner's.
- Open a checkpoint in OpenROAD or with `rb pnr-export --checkpoint`. It is never a final result. Old run directories are not pruned.

A run that fails in global route keeps that database as the `global_route` checkpoint even without `checkpoints`, getting a run directory if needed.

- It is marked `failed`, with copies of the congestion reports (`04_global_route.congestion.rpt`, `04_global_route.congestion-<i>.rpt`).
- The run still fails, and `failed_global_route` names `<run-id>/global_route`. `rb pnr-export --checkpoint` opens it, no `--resume` starts from it, and `checkpoint_stages` omits it.
- A failure a hook catches is dropped when a later global route passes or the stage ends.
- A database OpenROAD did not finish writing, or rb could not file, stays in `global_route_failed/` until the next run, named by `pnr.failed_global_route_not_kept`.

## Compare timing across stages

`stage_timing` events record each corner's slack at every stage end, so you can compare iterations stage by stage without your own reports. A run writing `progress.jsonl` (`checkpoints:` set, even to `[]`, `stages: true`, `--stop-after`, or post-route repair) appends one per stage end there and lists them under `timing` in `manifest.json`.

```json
{"event": "stage_timing", "stage": "global_route", "index": "04", "point": "stage_end", "parasitics": "global_route", "corners": {"tt": {"wns_setup_ps": -224.675, "wns_hold_ps": 343.534, "tns_ps": -322.268, "tns_hold_ps": 0.0}}}
```

| Field | Meaning |
| --- | --- |
| `stage`, `index` | The stage and its file index. |
| `point` | `stage_end`, `before_repair` or `round`. |
| `round` | The post-route round (0 is the detailed route); on the stage's `stage_end`, the kept round. |
| `parasitics` | `none` (no wire RC), `placement`, `global_route` (estimates) or `extracted` (SPEF). |
| `corners` | Per corner, in config order: `wns_setup_ps`, `wns_hold_ps`, `tns_ps`, `tns_hold_ps`, as in the result. A single-corner platform has one entry, named by `corner:`. |

| Stage | Events | `parasitics` |
| --- | --- | --- |
| `floorplan` | `stage_end` | `none` |
| `place`, `cts` | `stage_end` | `placement` |
| `global_route` | `before_repair` when [global-route repair](#repair-timing-after-routing) runs, then `stage_end` | `global_route` |
| `detailed_route` | `stage_end` | `global_route`, from the route guides |
| `post_route_repair` | One `round` per round, then `stage_end` copied from the kept round | `extracted` |
| `finish` | `stage_end` | `extracted` with `rcx-rules`, else `global_route` |

- The numbers match the stage's own report on the same parasitics (the `--stop-after` report; the final reports for `finish`), with more digits.
- A side with no constrained path has null WNS and TNS.
- A corner rb could not time has nulls and an `error`. This never fails the run; no result field or verdict reads the events.

## Run stages as separate sessions

`stages: true` runs each stage as its own OpenROAD session reading the previous stage's checkpoint, so you can stop early or retry a later stage. It writes every checkpoint, keeps each session's script and log there, and concatenates them into `pnr.tcl` and `pnr.log`. Results match a one-session run at the same thread count, with the exceptions in [Known Issues](../known-issues.md#a-staged-pr-run-can-differ-from-a-one-session-run).

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --stop-after global_route
rb pnr alu_pnr -c pnr/alu/pnr.yaml --resume latest --from detailed_route
```

- `--stop-after <stage>` works on any run. It writes that checkpoint and a stage report and passes with `routed: false`, with no routed outputs, GDS or abstract; `xfail:` does not apply.
- `--resume <run> --from <stage>` starts a new staged run from the checkpoint of the stage before `<stage>`. `<run>` is `latest`, a run id or a checkpoint directory. The old run is unchanged, so detailed routing can be retried from one global route.
- The resumed run copies the inherited checkpoints' reports (`grt_antennas.rpt`, `route.drc.rpt`, `route.maze.log`, `drt_antennas.rpt`) into `artefacts/<run>/`.

A resume is refused (`fail_stage: resume`) when an earlier stage's generated script or input files, or the OpenROAD version, changed, when the OpenROAD version does not start with a release number and so names no build, or when it resumes a probe into a closure run. [`--accept-change`](#accept-a-change-on-resume) accepts a named script or input change.

- A `constraints:` path and a map of that path to every corner count as different.
- Settings read only from `--from` on, such as `detailed-route-verbose`, routing layers or `threads:`, may change.
- Files sourced by hooks, `platform-tcl`, a `pdn-config` or the SDC are not checked unless listed under [`inputs:`](#run-tcl-hooks). Environment variables are not checked.

### Accept a change on resume

A resumed run equals a fresh run of the current configuration unless it records accepted changes. `--accept-change <change>`, repeatable, lets a resume proceed although an earlier stage's script or input differs, such as a `session` hook edited to affect only stages from `--from` on:

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --resume latest --from global_route \
  --accept-change pnr/alu/hooks/session.tcl
```

| Value | Accepts |
| --- | --- |
| `script:<stage>` | That stage's script. |
| `script` | Every differing stage script, such as all those `placement.rotated-macro-footprint` changes. |
| A path | That input file. Relative to where rb runs, or absolute; rb compares resolved paths. |

- The refusal lists every script and input difference, ending each acceptable one with its value. It prints paths relative to where rb runs when under it, else absolute.
- Only differences a value names are accepted. A value that names no difference, or only unacceptable ones, refuses the resume, saying which and listing what differs.
- The OpenROAD version, manifest, probe and stage-before-`--from` checks come first, and each refuses alone.
- Only an input a stage from `--from` on reads can be accepted; otherwise the refusal names the first stage that reads it.
  - The floorplan session alone reads the netlist, SDC, pin constraints, planner files, `pdn-config`, `tracks-tcl` and `tapcell-tcl`.
  - A hook is read only by its stages, so a changed `after-cts` hook cannot be accepted from `--from global_route`.
  - `session` and `stage-end` hooks, Liberty, LEF, other PDK Tcl and `inputs:` count as read by every stage.
- No value accepts another OpenROAD version, a probe resumed into a closure run, a missing or changed checkpoint file, an unfinished stage before `--from`, an unreadable `manifest.json`, a missing or unreadable input, or an input configured or dropped since (a file added to or removed from a list, or an `inputs:` entry at another path).

The run records each accepted change under `accepted_changes` in `manifest.json` and its result, and warns `pnr.resume_accepted_change`. A script record names the stage and first differing line; an input record names the role and the old and new path and digest. A hardened block's `abstract.manifest.json` records them too, and they do not make it stale, since staleness compares the files and configuration the run read.

Resuming the resumed run needs no value while the script or file is as accepted; the new run records the change with `inherited_from` naming the run that accepted it. Otherwise the refusal names it again and the earlier record is dropped.

## Dispatch the stages to Slurm

`--dispatch slurm` runs each stage as a Slurm job sized for that stage, held until the previous one succeeds, and implies `stages: true`. `--dispatch local` (default) runs stages in place. `rb pnr` waits on the submitting host, then checks the result and streams out GDS there, so give that host memory for KLayout.

```yaml
runs:
  - name: alu_pnr
    # ...
    threads: auto
    resources:
      cpus: 4
      mem: 8G
      time: "02:00:00"
      stages:
        detailed_route: {cpus: 16, mem: 32G, time: "12:00:00"}
```

- A job's reservation comes from `resources.stages.<stage>`, then the run's `resources:`, then `cfg-dispatch.resources`, then built-in defaults, with `pnr.dispatch_resources_default` warning when the run has no `resources:`.
- With `threads: auto` each stage uses its job's CPUs.
- Jobs are named `rb:pnr:<run>:<stage>`, and `cfg-dispatch.sbatch-args` apply to each.
- `checkpoints/<run-id>/dispatch/` holds each job's id, Slurm log, rtl_buddy log and result.
- A job that fails, times out or runs out of memory fails the run at that stage (`fail_stage: dispatch` unless the stage itself failed), and Slurm removes the later jobs.
- A job sbatch refuses because the queue is full (`MaxJobCount`) is resubmitted with backoff for up to 30 min (`dispatch.submit_deferred`; see [Dispatch](dispatch.md#troubleshoot-reservations-arrays-and-waits)).
- Ctrl-C, SIGTERM or SIGHUP cancels the jobs.
- If `rb pnr` is killed, its jobs keep running, and a later `rb pnr` of the same configuration refuses to start until they leave the queue, printing the `scancel` command. Without `squeue`, the refusal lapses once the dispatch directory has been quiet for the sum of the stage time limits (`pnr.stage_jobs_unchecked`), so check the queue first ([Known Issues](../known-issues.md#an-unconfirmed-dispatched-pr-run-blocks-later-runs)).

## Run several P&R calls at once

`rb pnr` locks the suite's whole `artefacts/` tree, so a second untagged call on the same `pnr.yaml` is refused, even for another run. Give each concurrent call its own `--run-tag`, for example to sweep one block's settings:

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --run-tag util55 &
rb pnr alu_pnr -c pnr/alu/pnr.yaml --run-tag util60 &
wait
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --run-tag util60 --png
```

A tagged call writes each run under `artefacts/.runs/<tag>/<run>/`, with its own lock and `rtl_buddy.log` in `artefacts/.runs/<tag>/`. Tag rules are in [Namespace concurrent runs](execution-context.md#namespace-concurrent-runs).

| Calls | Result |
| --- | --- |
| Two untagged, or two with the same tag | They share one lock; the second is refused. |
| Different tags | They run side by side. |
| One tagged, one untagged | Side by side, except while the tagged call copies an input from the untagged tree: it waits for that lock, logging `artifact_lock.wait` every five minutes. |
| A tagged call whose tree another call holds only to copy a block abstract out | It waits for the copy. |
| A whole-suite call pulling in a run from a suite whose tagged tree another call holds | Refused, naming that tree. |

- With no run name, the tag also covers runs pulled in from another `pnr.yaml`, which write and lock that suite's `artefacts/.runs/<tag>/`.
- `--resume latest` and `--dispatch slurm` work inside the tag's tree; checkpoints, stage jobs and orphan checks are per tag.

### What a tagged call reads

A tagged call reads nothing in place from a tree it does not own. It copies each block abstract, and the upstream synthesis netlist and filelist, into its own tree while holding the source tree's lock, and reads only the copies.

- Each read checks the copy against the content it was taken from, and refuses a changed copy, naming the directory to remove so it is copied again.
- Synthesis and P&R in one call read the same copies. A tagged run whose synthesis has no netlist to copy fails at setup.
- A block copy's manifest paths are checked against the block's own project.
- Copies live in `artefacts/.runs/<tag>/.block-abstracts/` and `artefacts/.runs/<tag>/.synth/`, one directory per source and content digest, never overwritten. They use disk until you remove the tag's tree (`rm -r artefacts/.runs/<tag>`), which also removes the results that read them.

A run's `blocks:` are copied from the abstract their block run published under the same tag.

- Only when `rb pnr` under the tag never started that block run is the untagged abstract copied, with `pnr.block_untagged_abstract`. This is checked under the tag tree's lock, so a starting block run is waited for.
- A block run the tag ran that failed, was cleared, or was skipped by `reglvl` in a whole-suite call fails its consumers.
- Result `blocks` entries, and a hardened block's `abstract.manifest.json` `blocks`, record the consumed abstract's `run_tag` (null for untagged) and `manifest_sha256`. A tagged run also writes them to `blocks.consumed.json`, with paths relative to the project root.
- Fix-it commands in block errors carry `--run-tag <tag>` when the abstract is tagged.

### Lock order

1. A tagged call locks its own tree first, so a duplicate call is refused before waiting for anything.
2. It then waits for the tagged tree of each other suite whose block it reads, copies the blocks and releases those trees.
3. It takes trees in one fixed order, releasing its own while waiting for one ordered before it. A consumer may wait for a same-tag call that holds a block's tree while producing it, but same-tag calls never wait for each other in a cycle.
4. Afterwards it holds at most one other tree's lock at a time. A tagged `--synth` copies its synthesis's block abstracts before locking the synthesis tree.

### Synthesis under a tag

Synthesis is not namespaced: it writes the untagged `artefacts/<synth>/`. Untagged, `--synth` locks the synthesis tree for the whole call; tagged, while each synthesis runs and its netlist is copied. When `synth.yaml` shares the directory of `pnr.yaml`, that tree is the untagged P&R tree, so a tagged `--synth` waits for any untagged call there to end.

### Export and read tagged results

`rb pnr-export --run-tag <tag>` exports a tagged result with the block copies recorded in its `blocks.consumed.json`, and fails when `blocks:` no longer lists one.

- It needs only those copies, so the block's `pnr.yaml` may have changed or gone, and the project may have moved or been copied.
- The copies may be in the tagged tree of the suite whose call pulled the run in, which the record names, even in another project.
- A record naming a copy outside the project and that tree, as an absolute path does after a move, is refused; re-run under the tag.
- Each checkpoint run directory keeps the record of the call that wrote it, which `--checkpoint` reads.

`rb power` and other readers read the untagged `artefacts/<run>/` only; see [`--run-tag` readers](../known-issues.md#--run-tag-covers-the-test-and-pr-flows-not-every-reader).

## Stream out GDS and PNG

`--gds` streams the routed DEF to GDS with KLayout after a successful run; `--png` also renders an image. Cell layouts come from the PDK's `cell-gds` and the run's `gds-paths`, where each macro's GDS goes (its LEF goes in `lef-paths`). A missing input stops the export.

A cell in the DEF with no layout is written as an empty placeholder, and `gds-mode` decides what that means:

- `preview` (default): the layout is kept and the run passes, with `gds_status: incomplete` and `gds_missing_cells` naming the cells. A KLayout failure only warns.
- `strict`: a missing cell, missing KLayout or `klayout-tech`, or export failure fails the run (`fail_stage: export`) and removes the GDS and PNG. The routed outputs stay.

List cells that are empty on purpose, such as ORFS's `fakeram45_*` macros, in `gds-allow-empty`.

## Export a saved result

`rb pnr-export` streams an existing result out again without rerunning OpenROAD, for example after installing KLayout:

```bash
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --png --gds-mode strict
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --checkpoint cts --png
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --png-only --lyp dark.lyp
```

- `--checkpoint` exports a stage checkpoint into `checkpoints/<run-id>/export/`, marked not final. `--def` exports another DEF with the run's platform. `--png-only` re-renders the existing GDS.
- The export never touches the routed results. Its verdict is the export alone: anything not delivered is `FAIL` with `fail_stage: export`.
- `export.provenance.json` records what it read and wrote.

## Harden a block

`harden: true` publishes a routed block as a hard macro for a larger design: `abstract/<top>.lef`, `<top>.lib` (timing only), `<top>.gds` and `abstract.manifest.json`, all or nothing (`fail_stage: abstract`).

```yaml
runs:
  - name: alu_block_pnr
    # ...
    platform: sky130hd_tt_block
    harden: true
```

- `<top>.lib` lists supply pins last, the same for staged and one-session runs. `<top>.params.json` records the parameter values; when Yosys cannot elaborate the top it is left out with `pnr.block_params_unrecorded`.
- It forces `--gds` with `gds-mode: strict`, so it needs KLayout and `klayout-tech`. `abstract: {gds: false}` publishes no GDS, records `null` in the manifest and leaves stream-out to `--gds`.
- It needs a single-corner platform.
- It turns on `buffer-ports`, so each port has one buffer and the abstract's pin load does not depend on the internals. Set the platform's `port-buffer`; on sky130hd OpenROAD otherwise picks a delay cell.
- The abstract LEF blocks every layer the block uses over its whole area. `abstract: {bloat: 0}` writes real shapes and a number grows them; `feedthrough-layers: [met4]` writes real shapes on those layers only, so the parent can route across. Check the parent's DRCs after changing it.
- A parent's `rb power` sees the block as 0 W; run `rb power` on the block.

The parent's top straps cross the block and drop vias onto its supply pins, so the block must leave the top layer free:

- Give the block its own PDK copy whose `pdn-config` straps stop below the parent's. On sky130hd the block uses met1 to met4 with pins on met4, and the parent's straps are on met5.
- Match the block platform's `routing-layers`. The template's `pdn_block.tcl` and `sky130hd_tt_block` show this.
- rb ties each block supply pin to the parent net of the same name, or to the only power or ground net.

A block that must use the top layer, such as one with an OpenRAM sky130 SRAM, exposes its supply pins there, and rb joins each to the parent strap of its net on the same track. For that:

- Build the block's top straps on the parent's width, pitch and net order.
- Place the block a whole number of pitches from the parent's strap offset.
- Define the parent's macro grids with `-cells` or `-instances` instead of `-default`.
- A pin rb cannot join fails the run with `block power: <inst>/<pin> ...`.

## Assemble hardened blocks

A top run names its blocks under `blocks:`, in `pnr.yaml` and in the `synth.yaml` entry it reads. The top's filelist must leave the module a blackbox (a port-only stub).

```yaml
# pnr.yaml
    blocks:
      - {name: alu_block, pnr: alu_block_pnr, pnr-path: ../alu/pnr.yaml}
# synth.yaml (pnr-path required, relative to synth.yaml)
    blocks:
      - {name: alu_block, pnr: alu_block_pnr, pnr-path: ../../pnr/alu/pnr.yaml}
```

- Build blocks first. `rb pnr -c top/pnr.yaml --synth` builds every block, its synthesis and the top in order from a clean tree. `-j N` hardens independent blocks in parallel, each with its own `threads:`.
- A run whose block failed is not attempted (`fail_stage: blocked`, with `blocked_by`).
- The block must use the top's technology LEF and corner.
- An abstract is refused, naming what changed, when its inputs changed since hardening or a major rb version with different built-in flow steps or defaults hardened it. Re-harden it, or pass `--accept-stale`.
- A block hardened with `abstract: {gds: false}` has no GDS. A parent's `strict` stream-out (its own `harden` and `rb pnr-export --gds-mode strict` included) fails at setup naming it. A `preview` stream-out, `--png` included, leaves it out of the layout and names it, as in [Stream out GDS and PNG](#stream-out-gds-and-png).

## Instance a parameterised block

A hardened block has one parameter set, its synthesis's `params:`. A parent that writes `blk #(.W(16)) u_blk (...)` needs a stub that declares the parameters:

```systemverilog
(* blackbox *)
module blk #(parameter int W = 8) (input logic clk, input logic [W-1:0] d, output logic [W-1:0] q);
endmodule
```

Use `frontend: slang` for such a parent. Before OpenROAD reads it, rb checks each instance against the block and rewrites it to `blk u_blk (...)`:

- every connected port must exist in the abstract with the same width;
- every input must be connected;
- each override must equal the value in `<top>.params.json`.

A mismatch stops the run naming the instance and both values. Re-harden a block that has no record.

## Flow reference

For maintainers and hook authors. The generated `pnr.tcl` runs:

1. `platform-tcl`, Liberty, LEF, a mode and scene per corner on a multi-corner platform, netlist, SDC, `max-fanout`, `layer-rc-tcl`, don't-use cells, `session` hooks. The SDC and `max-fanout` go into every mode.
2. Floorplan, tracks, blockages and cut-outs, tie cells, floorplan setup repair.
3. Macro placement, tap cells, power grid, pin placement, port buffering.
4. Global placement (with `lifetime: global-placement` and rotated-macro blockages present, and `hold-outputs-at-pins` buffers fixed), freeing those buffers, `repair_tie_fanout`, `repair_design`, legalization, `improve_placement`, `optimize_mirroring`, `placement.macro-pin-limits`, the place stage's `stage-end` hooks, `check_placement`. An illegal placement, including one a hook left, fails the stage, so no checkpoint is written and CTS does not start.
5. `repair_clock_inverters`, CTS, post-CTS setup repair, hold repair, legalization.
6. Global route, global-route repair, antenna repair, detailed route, antenna repair with detailed routes, the optional post-route repair stage, whose rounds each repair antennas after their detailed route.
7. Fill, global connections, extraction with `rcx-rules`, final reports, outputs.
8. After OpenROAD: the routed-floorplan check, then stream-out and abstract.

- `stage-end` hooks run at the end of each stage, after its last global connection; the checkpoint is written after them.
- A `probe` skips the setup and hold repairs and the antenna repair after global route, and stops after global route.
- Each session must print its end marker (`>>> DONE`, `>>> End of stage <NN>_<stage>`, `>>> STOPPED after <stage>`, or the post-route round markers), or the run fails `incomplete`. This check comes before any check of the design.

A staged session starts with a prelude from the same template: the header, Liberty and LEF, `read_db` and `read_sdc` of the previous checkpoint (every mode), its saved flow variables, then step 1's per-session settings. It keeps only what the database, the checkpoint SDC and the state file hold. A resume compares each earlier stage's script up to its end-of-stage line, ignoring comments, run-directory lines, `set_thread_count` and header variables the stage does not read.

Each stage's wall time, CPU time and peak memory are recorded as `stage_end` events in `progress.jsonl` and under `resources` in the manifest; in a one-session run a stage's peak includes what earlier stages left resident. Its [timing](#compare-timing-across-stages) goes to `stage_timing` events and `timing`.
