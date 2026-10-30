---
description: Run OpenROAD place-and-route on a synthesised netlist with rb pnr, read and debug the result, and control the floorplan, timing repair, long runs, GDS export and hardened blocks.
---

# Place-and-Route

`rb pnr` places and routes a netlist from `rb synth` with OpenROAD and reports area, timing, routing DRCs and electrical violations. Every `pnr.yaml` and platform field is defined in [YAML Formats](../reference/yaml.md#pnryaml); this page says when to use them.

## Run P&R on a block

You need OpenROAD 25Q1 or newer on `PATH` (or named in `cfg-pnr-tools`) and a synthesis run that has written its netlist. KLayout is needed only for GDS and PNG output. An older OpenROAD logs a warning and runs unvalidated.

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

Outputs go to `artefacts/<run>/` next to `pnr.yaml`; each run deletes the previous run's outputs first, except a refused `--resume`, which stops before that and leaves them in place. With no run name, `rb pnr` runs every entry whose `reglvl` is within `-l`, hardened blocks before the runs that use them.

## Interpret results

`PASS` means OpenROAD ran the flow to its end and wrote its outputs, or, for `mode: probe` and `--stop-after`, to the stage it stops after, with `routed: false` and no routed outputs. Check `routed` before treating a `PASS` as a routed result. It does not mean the design meets timing or routes clean unless [signoff gates](#gate-signoff) are set. Judge the design on these fields:

| Field | Meaning |
| --- | --- |
| `wns_setup_ps`, `wns_hold_ps` | Worst setup and hold slack. Negative misses timing. |
| `tns_ps`, `tns_hold_ps` | Total negative setup and hold slack. |
| `drc_count` | Violations left in `route.drc.rpt`. 0 is a clean route. |
| `max_slew_violation_count`, `max_capacitance_violation_count`, `max_fanout_violation_count` | Pins over a Liberty or SDC limit at any corner, listed in `electrical.rpt`. |
| `worst_path_startpoint`, `worst_path_endpoint`, `worst_path_levels`, `worst_path_resolved` | The worst setup path over every path group, at the setup-critical corner: its ends as RTL names where the netlist keeps them, its combinational cells, and whether both ends resolved. See [Find the critical path and loaded nets in the RTL](#find-the-critical-path-and-loaded-nets-in-the-rtl). |
| `max_net_fanout` | Sink pins of the signal net of highest fanout, clock, reset-tree and supply nets excluded. |
| `macro_pin_buffered_count`, `macro_pin_unmet_count` | With `placement.macro-pin-limits`: macro pins it buffered, and those still over their limit less the margin on placement parasitics. |
| `area_um2` | Design area. |
| `routed_cell_count` | Instances of the finished design, including added tie, buffer and clock cells, excluding physical-only cells. Compare experiments on this. |
| `cell_count` | Instances of the input netlist (the summary's Cells column). |
| `physical_cell_count` | Tap, endcap, spacer and fill cells. |
| `worst_setup_corner`, `worst_hold_corner`, `corners` | Multi-corner platforms: the corner behind each WNS, and each corner's four timing values. |
| `routed`, `mode`, `stopped_after`, `global_route_overflow` | `routed: false` means the run stopped early ([probe](#probe-a-floorplan) or `--stop-after`); its numbers are that stage's estimate, with the global-route overflow from `global_route` on. |
| `signoff` | With [`signoff:`](#gate-signoff): each gate's values and verdict. |
| `post_route_repair` | The rounds of [post-route repair](#repair-timing-after-routing): `kept_round`, `rounds`, `repaired_rounds`, `stop`, `reason`, `passed_over_round`, and with `electrical-repair` `electrical_round`, which, when the round is kept, lists in `unseen` the violators its `repair_design` judged within limits on the placement estimate. |
| `openroad_threads` | Requested and effective [thread counts](#openroad-threads). `effective` is null when `threads:` is unset. |
| `gds_status`, `gds_missing_cells` | [Stream-out](#stream-out-gds-and-png) outcome. |
| `checkpoint_dir`, `checkpoint_stages`, `last_step` | With [checkpoints](#keep-stage-checkpoints): where they are, which stages the run reached and checkpointed (never a failed global route), and the last step reached. |
| `failed_global_route` | `<run-id>/global_route`: the checkpoint holding a failed global route's database, for `rb pnr-export --checkpoint`. |
| `allow_congestion` | `true` on a [diagnosis run](#tune-global-routing) that let global route keep its overflow; `global_route_overflow` is then the final overflow. |

Times are in picoseconds, converted from the Liberty `time_unit`. The final timing uses extracted parasitics when the PDK sets `rcx-rules`, else the global-route estimate. A field rb could not read is left out.

A passing run's description carries qualifiers worth reading:

- `pnr.no_wire_rc`: placement and CTS ran without wire RC. See [Set wire RC](#set-wire-rc).
- `electrical N max-slew, ... violator(s)`: set `fail-on-electrical: true` or `electrical` in `signoff.fail-on` to fail such runs.
- `post-route repair ...`: a repair round was passed over or its session failed and was set aside.
- `post-route electrical round N kept`, `not kept` or `failed`: the [electrical round](#repair-electrical-violators-after-routing) ran.
- `GDS incomplete: ...`: cells with no layout in `preview` mode.
- `stale block abstract(s) accepted: ...`: run with `--accept-stale`.
- `global-route.allow-congestion (diagnosis only): global route overflow N`: global route was allowed to keep its overflow. See [Tune global routing](#tune-global-routing).

A run fails when OpenROAD exits non-zero, logs an `[ERROR ...]` line (except in a post-route repair session rb sets aside), stops before the end of its flow, or a check below fails. It is `SKIP` when `-l` filters it out or `tool:` is not `openroad`.

### Find the critical path and loaded nets in the RTL

The final reports write two reports that point into the RTL:

- `worst_paths.rpt`: the worst setup path of each path group (one per capture clock, plus `asynchronous` for recovery and removal checks and any `group_path` groups), with its slack, its logic levels and its startpoint and endpoint. On a multi-corner platform every group is timed at the setup-critical corner, the corner with the worst setup slack, which the file names. Logic levels count the cells on the path between the startpoint's cell and the endpoint's, neither counted, buffers and inverters included; the file gives how many of them are buffers or inverters. `timing.rpt` holds the full path.
- `fanout.rpt`: the signal nets of highest fanout, `report-fanout-nets` of them (default 10), highest first, each with its driver. Fanout is the net's sink pins: instance inputs and output ports. Fanout is a pin count, so it compares across PDKs where delays do not. Excluded are supply nets, clock nets (the STA clock network and nets CTS built), and reset-tree nets: those reaching a register's asynchronous set or clear pin, through any buffers and inverters before it.

Each point and driver is given by its RTL name where the synthesis netlist keeps one, tried in this order:

1. A top-level port, by its name.
2. A pin of an instance the netlist names itself, such as a macro or a hardened block: `u_core.u_sram.din0[3]`.
3. The net that names the pin, when the netlist declares it under a name of its own. For a startpoint or a driver that is the net its pin drives. For an endpoint it is the register's output when the cell has exactly one (Liberty clock-to-output arc) that is not a clock, so a register's endpoint reads as the register, such as `u_core.state_q[3]`; otherwise it is the net on the endpoint pin.
4. An instance with a Yosys `src` attribute, by its scope and source lines: `u_core (alu.sv:42)`.

A name made up by Yosys (`_123_`, a name with `$`) is not one of the netlist's own. A driver that P&R inserted is traced back through buffers and inverters to the first driver that resolves, and the file says how many it is behind, and `inverted` when an odd number of them are inverters. A name that does not resolve stays as OpenROAD prints it, marked `(unresolved)`, and the file gives the raw name and `src` beside every resolved one. `worst_path_resolved` is `false` unless both ends of the worst path resolve. If the reports cannot be written, `pnr.path_report_failed` warns and their fields are left out; the run's verdict does not change.

A value that cannot be computed, because a report's commands failed or found nothing, is left out of the result and is `unavailable` in its file, with the reason, never 0. After the timing the final reports already run, the reports take about 0.4 s on a routed design of 373,000 instances and 60,000 nets: the fanout scan grows with the net count and the path search with the number of path groups.

## Inspect artefacts

| File in `artefacts/<run>/` | Contents |
| --- | --- |
| `pnr.log`, `pnr.tcl` | OpenROAD output, written live, and the generated flow script |
| `<top>.def`, `<top>.routed.odb` | Routed design. `rb power` reads the ODB. |
| `<top>.routed.v`, `<top>.routed.sdc` | Logical netlist (no supply ports or supply pin connections) and constraints, for STA and gate-level simulation. The SDC is the primary corner's on a multi-corner platform. |
| `<top>.routed.pwr.v` | Powered netlist, for LVS against `<top>.gds`: the top's supply ports, last and sorted by name, and every instance's supply pins, fill and tap cells included, connected by the `pdn-config` global connections. It is the same whether the run was staged or one session. |
| `<top>.<corner>.routed.sdc` | Each corner's constraints, on a [multi-corner platform](#sign-off-at-several-corners) |
| `<top>.routed.spef` | Extracted parasitics, when the PDK sets `rcx-rules` |
| `timing.rpt`, `electrical.rpt` | Worst paths and electrical violators, all corners |
| `worst_paths.rpt`, `fanout.rpt` | Each path group's worst setup path and the highest-fanout signal nets, with RTL names. See [Find the critical path and loaded nets in the RTL](#find-the-critical-path-and-loaded-nets-in-the-rtl). |
| `route.drc.rpt`, `route.maze.log` | Detailed-route DRCs and router log |
| `grt_antennas.rpt`, `drt_antennas.rpt` | Antenna violations left after the antenna repair after global route and after detailed route; with `post-route-repair`, `drt_antennas.rpt` is the kept round's |
| `congestion.rpt` | Overflowing global-route tiles, only when global routing overflows. Loads in the OpenROAD GUI's DRC viewer. |
| `congestion-<i>.rpt` | The same at iteration `i`, every `global-route.congestion-report-iter-step` iterations of an overflowing route |
| `rtlmp/` | RTL-MP's reports, when it placed macros |
| `<top>.gds`, `<top>.png`, `klayout.*.log`, `def2stream.inputs.json` | Stream-out outputs, logs and the inputs it read |
| `checkpoints/`, `abstract/` | [Stage checkpoints](#keep-stage-checkpoints) and a [hardened block's](#harden-a-block) views |

When OpenROAD fails, the run removes the routed database so `rb power` never reads a stale one; `pnr.log`, `pnr.tcl` and the congestion reports stay. A failed global route also leaves its database as a [checkpoint](#keep-stage-checkpoints). A run that fails a later check (`routed_check`, `fail-on-electrical`, a signoff gate, `export`, `abstract`) keeps its routed outputs for inspection.

## Follow a running flow

- `tail -f artefacts/<run>/pnr.log`. Each step starts with a `>>> ` line.
- Global route prints a per-layer usage table (`GRT-0096 Final congestion report`).
- Detailed route logs each iteration (`DRT-0195`) and its violation count (`DRT-0199`), so a converging route can be told from a stalled one. `detailed-route-verbose: 0` silences this; higher levels add detail.
- With [checkpoints](#keep-stage-checkpoints), `checkpoints/latest/progress.jsonl` records each step, hook and stage. After a kill, the last `step_begin` without a `step_end` is where the run was. Its `stage_timing` events give each corner's slack at every stage end; see [Compare timing across stages](#compare-timing-across-stages).

## Fix a failed run

Read the row's description and `fail_stage` first, then `pnr.log`. A result with any `fail_stage` failed before or instead of a verdict on the design, so `xfail:` never excuses it. A failure without `fail_stage` (an OpenROAD error, a don't-use violation, `fail-on-electrical`, a [signoff gate](#gate-signoff)) can be excused.

| Symptom | Cause and fix |
| --- | --- |
| `fail_stage: setup` | Configuration or input problem found before OpenROAD started: a missing file (`pdn-config`, `rcx-rules`, a hook, a planner file), a bad planner-file entry, `post-route-repair` without `rcx-rules`, blockages on OpenROAD older than 26Q1, `cts-obstruction-aware: false` on an OpenROAD whose CTS has no `-no_obstruction_aware`, a per-corner `constraints:` that does not fit the platform, or an OpenROAD that does not answer the [SDC mode check](#sign-off-at-several-corners). The description names it. |
| `fail_stage: incomplete`, `the flow did not finish` | A Tcl file the flow sources (PDK Tcl, `pdn-config`, `pin-constraints`, a hook) called `exit` or `return -level 2`, ending OpenROAD with code 0. The description names the last step reached; remove the exit from the file that step sources. |
| `fail_stage: routed_check` | A located macro or exact pin moved between floorplan and the routed DEF, usually because a `pin-constraints` file or hook moved it. |
| `fail_stage: pins_complete` | `pins-complete: true` found a port with no pin entry, or with conflicting entries. Add or remove entries. |
| `fail_stage: checkpoint` | A checkpoint could not be written (disk full; see the `checkpoint` event in `progress.jsonl`), or a hook wrapped `global_placement`, `clock_tree_synthesis`, `global_route` or `detailed_route` with `rename` in a one-session run. Free space, or remove the wrapper or set `stages: true`. |
| `fail_stage: resume` | `--resume` refused: a stage script, input file, checkpoint or OpenROAD version differs. Rerun from an earlier stage. |
| `fail_stage: dispatch` | A Slurm stage job ended `OUT_OF_MEMORY`, `TIMEOUT`, `CANCELLED` or similar, never started, or was refused. Read `dispatch/<NN>_<stage>.slurm.log`; raise that stage's `resources:`. See [Dispatch](#dispatch-the-stages-to-slurm). |
| `fail_stage: abstract` | A hardened block's view could not be written. Read `pnr.log`. |
| `fail_stage: outputs` | rb could not read, decode or rewrite `<top>.routed.pwr.v` or a hardened block's `<top>.lib` to put the supply ports last. The description names the file and error. |
| `fail_stage: export` | Stream-out failed, under `gds-mode: strict` in `rb pnr` or in any mode in `rb pnr-export`: KLayout, `klayout-tech`, an input file or the render. Fix it and run `rb pnr-export`. |
| `fail_stage: blocked`, `synth`, `error` | A block this run uses failed, its upstream synthesis failed under `--synth`, or rb crashed (the exception is in the description). Fix the cause first. |
| `[ERROR PDN-0179] Unable to repair all channels` | Macros too close for the power grid. Set `placement.macro-channel`. |
| `macro ... overlaps floorplan.blockages[N]` (or `floorplan.core-cutouts[N]`) | A placed macro sits on a `hard` blockage or core cut-out. Fix the macro with a `floorplan.macros` location, enlarge `placement.macro-channel`, or move the blockage. |
| `PDN-0008 ... halo overlaps row` | A `pdn-config` macro grid's `-halo` reaches a standard-cell row the macro does not sit on. Cut the rows around each macro at least that far; see [Plan the power grid](#plan-the-power-grid). |
| `PDN-0233` on a turned macro or a block's grid | The power grid has no via for that macro. See [Place macros](#place-macros) and [Harden a block](#harden-a-block). |
| `GRT-0116`, global-route overflow | Too congested. The result's `failed_global_route` names the checkpoint (`<run-id>/global_route`) that holds the failed route's database and a copy of `congestion.rpt`; open it with `rb pnr-export --checkpoint` or `read_db` in OpenROAD. Lower utilization or density, try `placement.routability-driven`, or [tune global routing](#tune-global-routing). Congestion between macros or between a macro and the core edge means RTL-MP left no channel there: set `placement.macro-channel`, such as ORFS's `[80, 80]` on sky130hd. |
| Large `drc_count` | Try `routing-layer-adjustment`, a lower density, or `post-route-repair` with several rounds. |
| `RSZ-0090` at a slow corner | A macro with one Liberty limits that corner. Map each corner to the macro's Liberty for it in `lib-paths`; see [Sign off at several corners](#sign-off-at-several-corners). |
| Macro pins in `electrical.rpt`, or `pnr.macro_pin_limits_unmet` | Set `placement.macro-pin-limits`, add a stronger buffer to it, or lower the macro's `macro-cell-halo`. See [Tune placement](#tune-placement-clock-tree-and-repair). |
| Output pins over the library's slew limit at signoff, yet no max-slew violators | Ports have no limit unless the SDC sets one, and `repair_design` does not repair them. See [Check and repair slew at output ports](#check-and-repair-slew-at-output-ports). |
| `RSZ-0074` in a post-route repair round | Set `post-route-repair.reroute: full`. |
| `RSZ-0089 Could not find a resistance value for any corner` | A setup repair ran on zero wire RC. Set the PDK's `layer-rc-tcl`; see [Set wire RC](#set-wire-rc). The result's description says so. |
| `CTS-0080 Sink not found` | Set `cts-sink-clustering: false`. On a block with macros, `cts-obstruction-aware: false` also avoids it. |
| `GRT-0273 Disabled NDR`, large hold violations | Clock nets with non-default rules detour. Set `cts-apply-ndr: none`. |
| `DPL-0011` | A macro placed off the row grid (`macro-snap: false`). Put its bottom edge on a row. |
| `IFP-0039` at `make_tracks` (ASAP7) | Set the PDK's `tracks-tcl`. |
| `RB-DONT-USE-VIOLATION` | The routed design uses an excluded cell, usually one the netlist already had. Change the RTL, synthesis or pattern. |
| `STA-0122 cell '<pattern>' not found` | A `dont-use-cells` or `cts-dont-use-cells` pattern matches nothing. |
| `openroad.threads_capped` | `threads:` exceeds the allocation. Raise the reservation or use `threads: auto`. |
| `floorplan.pins[i]: no port matches`, `floorplan.macros[i]: no instance matches` | Fix the name or glob. |
| `floorplan.macros: the netlist has no hard macros` | Located macros place nothing; check that synthesis kept the macro instances. |
| Missing or stale block abstract, platform/corner mismatch | Run the `rb pnr` command the message names, or `rb pnr` with no run name. |
| `block power: ...` | A block's top-layer supply pins miss the parent's straps. See [Harden a block](#harden-a-block). |
| `port 'd' is connected to N bit(s)`, `instance ... sets <P>=...` | The parent instances a block with other ports or parameters than it was hardened with. See [Parameterised blocks](#instance-a-parameterised-block). |
| Run refused: earlier dispatched jobs still queued | Wait, or run the `scancel` it prints. If `squeue` cannot answer, remove the `dispatch/` directory it names once the jobs have ended. |

## Configure the physical platform

A PDK entry in `cfg-pdks` supplies the files and cell names; a `cfg-pnr-platforms` entry selects a PDK, its corner or corners, and the per-platform knobs. A run picks a platform with `platform:`. P&R reads these PDK keys beyond the ones synthesis uses:

| PDK key | Use |
| --- | --- |
| `tie-hi`, `tie-lo`, `fill-cells`, `pin-layers` | Tie cells, filler masters, horizontal and vertical pin layers. `pin-layers` is required: `place_pins` has no default layers |
| `cell-gds`, `klayout-tech`, `klayout-props` | Stream-out layout and KLayout technology and colours |
| `pdn-config` | Tcl that declares the power grid (no `pdngen` call). Unset means no grid. |
| `rcx-rules` | OpenRCX rules. Enables extraction, `<top>.routed.spef` and post-route repair. Its `LayerCount` must match the LEF. |
| `layer-rc-tcl` | `set_layer_rc` / `set_wire_rc` lines. Sourced after `read_sdc`. See [Set wire RC](#set-wire-rc). |
| `platform-tcl` | Sourced before the first Liberty read, such as `suppress_message` lines |
| `tracks-tcl` | Replaces the bare `make_tracks` after floorplan initialisation |
| `tapcell-tcl` | Inserts tap and endcap cells, after macro placement and before the power grid. Unset runs `cut_rows`, which cuts the rows 2 µm around each placed macro |
| `dont-use-cells` | Cells no step may choose. Also read by synthesis. |
| `placement.*` | Placement defaults, overridable per platform and per run |

The four Tcl keys match ORFS's `PLATFORM_TCL`, `SET_RC_TCL`, `MAKE_TRACKS` and `TAPCELL_TCL`. They are sourced as written, so a file copied from ORFS must not read ORFS environment variables. A missing file fails the run at setup.

`dont-use-cells` takes one name or glob per entry, with `*` and `?` only. A platform's list adds to its PDK's and applies to its own flow only. After routing, any instance of an excluded cell is printed as `RB-DONT-USE-VIOLATION: <instance> <master> <pattern>` and fails the run.

## Sign off at several corners

Give a platform `corners: [tt, ss, ff]` instead of `corner:`. All corners are analysed in one OpenROAD session, so repair and the final reports see every corner. Clock-tree synthesis uses the first, the primary. Without either key the PDK's first corner is used. `rb power` reads the list too, in one SDC mode ([Known Issues](../known-issues.md#rb-power-times-every-corner-in-one-sdc-mode)); `rb synth` stays single-corner.

Each corner is timed in an OpenSTA SDC mode and scene of its own, named after the corner, so a generated clock's latency comes from the corner being timed:

- The SDC, `max-fanout` and the propagated clocks after CTS apply in every mode. The final reports read the routed SPEF once per corner.
- `constraints:` can give each corner its own SDC, as a map that names every corner. Define the same clocks in each, since CTS builds the clock trees from the primary corner's.

  ```yaml
      constraints: {tt: top_tt.sdc, ss: top_ss.sdc, ff: top_ff.sdc}
  ```

- The primary corner's mode is the one CTS, hooks and an SDC command without a mode use. To constrain every corner from a hook, run the command after `set_mode <corner>` for each corner, then `set_mode <primary>`.
- `<top>.routed.sdc` holds the primary corner's constraints and `<top>.<corner>.routed.sdc` each corner's. A checkpoint's `.sdc` holds every mode; its `<NN>_<stage>.<corner>.sdc` holds one corner's as a plain SDC.
- `max_fanout_violation_count` counts a pin once per corner it violates at. Without modes (below) it counts the pin once.

A mode per corner needs OpenROAD 26Q4 or newer; rb asks OpenROAD at setup. An older one times every corner in one mode and warns `pnr.corner_modes_unsupported`, and a per-corner `constraints:` fails setup. An OpenROAD that gives no answer twice fails setup with `pnr.corner_modes_probe_failed`. See [Known Issues](../known-issues.md#corners-share-one-sdc-mode-on-an-openroad-without-modes).

A `lib-paths` entry that is one path is timed at every corner, so a macro keeps that file's timing at every corner and can fail a slow corner (`RSZ-0090`). An entry can instead map each corner to the macro's Liberty for it:

```yaml
    lib-paths:
      - {tt: macros/sram_tt.lib, ss: macros/sram_ss.lib, ff: macros/sram_ff.lib}
```

Each file is timed at its corner only. A map must name every corner of the platform and no other, or setup fails with `pnr.lib_paths_corners`; on a single-corner platform it names that one corner. The Liberty time-unit check reads every file, and `--resume` refuses a checkpoint made with another map. A file listed both as a plain entry and in a map fails setup with `pnr.lib_paths_duplicate`, because OpenSTA times a cell with the last copy it read; drop the plain entry. `rb power` reads a map inherited from the P&R run or in its own `lib-paths`, and accepts a platform with fewer corners than the map; see [Power](power.md).

A multi-corner platform cannot harden a block or consume hardened blocks.

## Gate signoff

A `signoff:` block on the platform or the run names the corners each timing side is signed off at and the checks that fail a routed run:

```yaml
    signoff:
      setup-corners: [ss]                  # default: every corner
      hold-corners: [ff]                   # default: every corner
      fail-on: [timing, drc, electrical]   # default: none
```

- `timing` fails when setup WNS over `setup-corners` or hold WNS over `hold-corners` is negative or was not reported. The final reports print two digits, so a WNS of `-0.00` is negative and `0.00` passes. Setup with no constrained path at every one of its corners fails, since nothing was checked; hold in that case passes.
- `drc` fails when `drc_count` is above 0, or when the detailed route wrote no DRC report (`drc.count` is then null). A clean route writes an empty report.
- `electrical` fails on any max-slew, max-capacitance or max-fanout violator, or when one of the three counts was not reported. `fail-on-electrical: true` adds this gate to `fail-on`, with or without the block.

A run's block overrides the platform's field by field, and `null` sets a key to its default or turns the block off. A corner the platform does not analyse fails the platform's load, or the run's setup.

The result's `signoff` field gives each gate its values and a `verdict` of `pass`, `fail` or `not_gated`: `timing.setup` and `timing.hold` with `corners`, `wns_ps`, `worst_corner` and `unconstrained`, `drc.count`, and the three electrical violator counts. `wns_setup_ps` and `wns_hold_ps` stay the worst over every corner. A failed gate fails the run without `fail_stage`, so `xfail:` can excuse it, and the description names each gate, such as `signoff failed: timing (setup WNS -0.1200 ns at ss); drc (3 violation(s))`. When `fail-on-electrical: true` is set, the electrical failure keeps the description of a run without the block, `electrical violators in the routed design: ... (fail-on-electrical; see electrical.rpt)`, after the `signoff failed:` prefix. The routed outputs stay. A `--stop-after` or probe run reports the values its stage has and gates nothing.

On a multi-corner platform the [post-route repair rounds](#repair-timing-after-routing) judge setup only at `setup-corners` and hold only at `hold-corners`. Without the block they judge every corner, and the result has no `signoff` field.

## Check and repair slew at output ports

A port carries no max-slew or max-capacitance limit unless the SDC sets one, so the electrical counts and the `electrical` gate pass a long net that ends at an output pin, however slow its pin. `repair_design` does not repair a port's slew even when the SDC sets a limit on it; it checks the net at its driver only. To make outputs part of the electrical check and have P&R fix them, do both:

- In the SDC, set `set_max_transition` on the outputs, for example `set_max_transition 320 [all_outputs]` at the library's limit, and `set_load` for what the pins drive. The violator counts and `electrical.rpt` then include the output pins.
- Buffer the outputs and hold the buffers at their pins:

```yaml
    buffer-ports:
      inputs: false                # default true
      skip-clock-outputs: true     # default false
      hold-outputs-at-pins: true   # default false
```

Each output net then ends at a buffer input, which has a Liberty limit, so placement's `repair_design` resizes the driver or adds repeaters to meet it. Without `hold-outputs-at-pins`, global placement puts each output buffer anywhere between its driver and its pin, and the net from the buffer to the pin is again a net that ends at a port. With it, each buffer goes on the row site nearest its pin that no other held buffer uses, after pin placement, and stays there, fixed (`FIRM`), through global placement. It is freed right after, before `repair_tie_fanout` and the `after-global-placement` hooks, so `repair_design` repairs the driver net and legalization places the buffer. That repair works on placement parasitics, so a long driver net can still route over its limit at the buffer's input; `electrical.rpt` lists it as a max-slew violator on the buffer's input pin.

`buffer_ports` also buffers an output that carries a clock: a forwarded clock on the clock network, or an output a generated clock is defined on. `skip-clock-outputs: true` leaves every such output unbuffered and names each in `pnr.log`.

`buffer-ports: true`, and the default of a `harden: true` run, buffer every input and output as OpenROAD and ORFS do, without the two options. `inputs` and `outputs` map to `buffer_ports -inputs` and `-outputs`. See [YAML Formats](../reference/yaml.md#pnryaml).

## Set wire RC

Set the PDK's `layer-rc-tcl`. Without it, placement, clock-tree synthesis and hold repair see the technology LEF's layer RC, which is zero on most open PDKs, and the routed design can fail hold that repair never saw. Setup repair cannot run on zero wire RC: OpenROAD stops with `RSZ-0089`, and the failed run's description names the cause and both fixes. Three steps repair setup by default, as ORFS does: `floorplan-setup-repair`, `post-cts-setup-repair` and the setup repair of `global-route-repair`. Without wire RC, all three must be off: `floorplan-setup-repair: false`, `post-cts-setup-repair: false`, and a `global-route-repair` without `setup-margin`, such as `{hold-margin: 0}`, or `null`. rb warns `pnr.no_wire_rc` and qualifies the `PASS` when the PDK sets no `layer-rc-tcl` or `pnr.log` has `EST-0018` (zero wire capacitance) or `CTS-0104` (zero clock wire RC). Without `layer-rc-tcl`, `session` hooks may set the wire RC instead: each session then checks after the hooks that every corner has signal and clock wire capacitance, and the run is not qualified for the missing `layer-rc-tcl` when every check passes.

For sky130hd, copy the `set_layer_rc` lines of OpenROAD's `test/sky130hd/sky130hd.rc` and pick the wire layers:

```tcl
set_layer_rc -layer met1 -capacitance 1.72375E-04 -resistance 8.929e-04
# ... one line per layer and via
set_wire_rc -signal -layer met2
set_wire_rc -clock -layer met5
```

The default [global-route repair](#repair-timing-after-routing) repairs again on the global-route estimate, which sees the wire length that placement estimates miss.

## PDK setup notes

- **Nangate45.** Set `pin-layers`, for example `metal3` / `metal2`. It has no `pdn-config`, so runs have no power grid. For extraction use ORFS `flow/platforms/nangate45/rcx_patterns.rules`.
- **sky130hd.** The template's `sky130hd` entry is a worked example: `pin-layers` `met3` / `met2`, `met*` routing layers, a `pdn-config`, `placement.density: 0.60`, a `dont-use-cells` list for the probe and `lpflow` cells, a `cts-buffer` list, and ORFS `sky130hs/rcx_patterns.rules`. Set `port-buffer` for hardened blocks.
- **ASAP7.** Set the four Tcl keys to ORFS's `liberty_suppressions.tcl`, `setRC.tcl`, `openRoad/make_tracks.tcl` and a copy of `openRoad/tapcell.tcl` with `TAP_CELL_NAME` and `MACRO_ROWS_HALO_X` / `_Y` filled in. List each corner's split Liberty files under that corner. List each threshold flavour's cell LEF (`asap7sc7p5t_28_R_1x_*.lef`, `..._L_...`) in `macro-lef`, which takes a list read in order, and macro LEFs in `lef-paths`. Give `fill-cells` each flavour's fillers too, or fill insertion fails with `DPL-0050`. In a Tcl hook, `$MACRO_LEF` names only the first LEF of the list. ORFS uses `routing-layer-adjustment: 0.25`.

## Tune placement, clock tree and repair

Process-dependent knobs live on the PDK, can be overridden on the platform, and most also on a run, so runs of one block on one platform can differ without a platform per variant. Which level takes each key is in the [YAML reference](../reference/yaml.md#synthesis-and-physical-design-tools). A default is OpenROAD's own or the setting ORFS uses, unless it belongs to an rb step without a counterpart, such as `placement.macro-pin-limits`, or works around a [known issue](../known-issues.md). Start from the problem:

| Problem | Knob |
| --- | --- |
| Global route overflows | Lower `placement.density` (default 0.7) or `floorplan.utilization`; `placement.routability-driven: true` (add `routability-use-grt: true` for a slower, closer estimate); a fixed `placement.reference-hpwl` such as `446000000` spreads a large design further. |
| DRCs left in congested areas | `routing-layer-adjustment` (0 to 1) withholds that fraction of each signal layer from the global router; `global-route.layer-adjustment` sets it per layer. See [Tune global routing](#tune-global-routing). |
| Setup misses on long nets | `placement.timing-driven: true`. On OpenROAD 26Q4 it also buffers and resizes during placement, which grows area. Needs wire RC. |
| Timing misses after routing | [`global-route-repair` or `post-route-repair`](#repair-timing-after-routing) |
| Long nets that placement's `repair_design` leaves for routing to push over their slew limit | `placement.repair-design: {max-wire-length: <µm>}`, below |
| Many fanout buffers on a library without a fanout limit (sky130hd) | `max-fanout: 100000` stops buffering for fanout alone; `repair_design` otherwise uses 50. |
| Cells too tight | `placement.padding` (sites, default 0) |
| Clock tree | `cts-buffer` (CTS picks the root buffer from it unless `cts-root-buffer` names one), `cts-sink-clustering`, `cts-obstruction-aware`, `cts-apply-ndr`; `cts-clock-nets` builds trees for the named clocks only |
| A strong root buffer with small branch buffers | `cts-root-buffer` names the root; `cts-buffer` lists the branch buffers only |
| Clock pins far from the nearest leaf buffer, as when many clock-gate cells sit in a small area, with slew violations on them | `cts-sink-clustering` with `size` and `max-diameter` (µm) |
| Setup misses from flops on a clock branch that CTS padded to match the deepest one (`CTS-0033` and `CTS-0036` in the log), as when a clock drives a buffer as well as flops | `cts-latency-balancing: false` |
| Long clock wires after CTS | `cts-repair-clock-nets: true`, or `{max-wire-length: <µm>}` to cap the wire length |
| A block must leave the top layers to its parent | `routing-layers` on the run |
| A block needs other tap spacing or endcaps than the PDK's | `tapcell-tcl` on the platform or the run |
| Cells crowd macro edges, or Metal Spacing DRCs at a block edge | `placement.macro-cell-halo` (µm, default 0), or a `tapcell-tcl` that cuts the rows around macros |
| Tie cells too close to their loads | `placement.tie-separation` (µm, default 0) |
| Macro pins over a tight Liberty limit, such as OpenRAM SKY130 SRAM inputs (0.04 ns) | `placement.macro-pin-limits`, below |

A platform that ends up with `routability-use-grt` but not `routability-driven` fails to load.

On a library with several threshold-voltage flavours, such as ASAP7 RVT and LVT with both flavours' Liberty in each corner and both cell LEFs in `macro-lef`, resizing moves cells between flavours freely. `keep-sizing-vt: true` keeps each cell's flavour when `repair_design` or `repair_timing` resizes it, so a cell changes flavour only through timing repair's threshold-voltage swap. `cts-dont-use-cells` keeps cells out of the clock tree only: CTS and `repair_clock_nets` skip them, and post-CTS repair may still place them on data paths. After `repair_clock_nets` the flow frees only the cells the list made dont-use; a matching cell that Liberty `dont_use`, `dont-use-cells` or an earlier hook excluded stays excluded. Neither CTS nor `repair_clock_nets` checks the `cts-buffer` or `cts-root-buffer` cells against either list. The list governs the cells CTS picks itself: dummy-load inverters, and delay buffers it infers. Name the buffer flavour in `cts-buffer` and `cts-root-buffer`. To keep a flavour out of every step, list it in `dont-use-cells`.

`match-cell-footprint: true` on the PDK or platform adds `-match_cell_footprint` to every `repair_design` and `repair_timing` the run emits: after placement, the floorplan and post-CTS repairs, global-route repair, and each post-route repair round, the electrical round included. Resizing then picks only cells with the same footprint, as ORFS does for platforms that set `MATCH_CELL_FOOTPRINT`, sky130hd and ihp-sg13g2 among them. It does not reach a `repair_design` or `repair_timing` a hook runs; pass the flag there yourself. rb runs no `repair_timing -recover_power`, which ORFS also gives the flag.

Each CTS control sits on the platform and on the run, and a run's value replaces the platform's whole:

```yaml
    cts-buffer: [sky130_fd_sc_hd__clkbuf_4]
    cts-root-buffer: sky130_fd_sc_hd__clkbuf_16
    cts-sink-clustering: {size: 20, max-diameter: 25}
    cts-obstruction-aware: false
    cts-repair-clock-nets: {max-wire-length: 1000}
```

This calls `clock_tree_synthesis -root_buf sky130_fd_sc_hd__clkbuf_16 -buf_list sky130_fd_sc_hd__clkbuf_4 -sink_clustering_enable -sink_clustering_size 20 -sink_clustering_max_diameter 25 -no_obstruction_aware`, then `repair_clock_nets -max_wire_length 1000`. `cts-obstruction-aware: false` needs an OpenROAD whose `clock_tree_synthesis` takes `-no_obstruction_aware`, which rb checks at setup; see the [YAML reference](../reference/yaml.md#synthesis-and-physical-design-tools).

`placement.repair-design` passes `slew-margin`, `cap-margin` (percent) and `max-wire-length` (µm) to the place stage's `repair_design`, as `global-route-repair.repair-design` does after global route. Unset, it runs with OpenROAD's defaults, as ORFS does. The block merges field by field from the PDK through the platform to the run, and `null` clears the levels below:

```yaml
    placement:
      repair-design: {max-wire-length: 600, slew-margin: 10}
```

`repair_design` judges wires on the placement estimate, which a long routed net can exceed. `max-wire-length` splits any wire longer than it with a buffer, so one driver does not carry a long chain to routing, where post-route repair may not see it (see [Known Issues](../known-issues.md#post-route-repair_design-sizes-its-buffering-on-the-placement-estimate)).

`repair_design` sizes repeaters for the library's target slew, so a macro pin whose own limit is far below it stays in `electrical.rpt`. `placement.macro-pin-limits` buffers such pins at the end of the place stage, after `repair_design`, the `after-repair-design` hook and legalization:

```yaml
    placement:
      macro-pin-limits:
        buffers: [sky130_fd_sc_hd__buf_1, sky130_fd_sc_hd__buf_2, sky130_fd_sc_hd__buf_4,
                  sky130_fd_sc_hd__buf_8, sky130_fd_sc_hd__buf_12, sky130_fd_sc_hd__buf_16]
        margin: 0.05              # of each max-slew limit, kept in hand
        capacitance-margin: 0.5   # of each max-capacitance limit; default: margin
```

Each macro data pin over its limit less the margin gets a buffer fixed on the free row site nearest it (an input also gets a pre-driver), sized from `buffers` weakest first and marked `dont_touch`, so later repair leaves it alone. When no pair meets the limit less the margin, the pair closest to it stays (for an input, the lowest slew), which is not always the strongest, and the pin counts in `macro_pin_unmet_count`. `pnr.log` has one `>>>` line per pin. The step judges on placement parasitics, so check `electrical.rpt`: a long macro-output net can route to twice its estimated capacitance (hence `capacitance-margin: 0.5` on sky130hd), a large `macro-cell-halo` keeps the buffer too far from the pin (3 µm works with `buf_8` on sky130hd, 20 µm does not), and a pin route that global routing detours around the macro is out of its reach. A block set on the run or platform replaces a lower level's whole.

### Optimisation steps

The flow runs the optimisation steps ORFS runs by default. Each has a key on the platform and the run that turns it off:

| Step | Where | Key |
| --- | --- | --- |
| `repair_timing -setup` with buffer removal and sizing only | End of the floorplan, before the `after-floorplan` hooks | `floorplan-setup-repair` |
| `improve_placement`, then `optimize_mirroring` | After the placement stage's legalization | `placement.improve-placement`, `placement.optimize-mirroring` |
| `repair_clock_inverters` | Before CTS, after the `before-cts` hooks | `cts-repair-clock-inverters` |
| `repair_design`, then setup and hold `repair_timing`, on global-route parasitics | After global route | `global-route-repair: null` turns it off, and a written block replaces it; see [Repair timing after routing](#repair-timing-after-routing) |
| `repair_antennas` (5 iterations), `check_antennas` into `grt_antennas.rpt` | After the global-route repair and its hooks | `global-route-antenna-repair` |
| `repair_antennas` and a detailed route while violations remain (at most 5), `check_antennas` into `drt_antennas.rpt` | After detailed route, and after each [post-route repair](#repair-timing-after-routing) round's detailed route, before its `stage-end` hooks | `detailed-route-antenna-repair` |

Turning a step off gives up what it does: the floorplan and global-route repairs recover setup slack, `improve_placement` and `optimize_mirroring` shorten wires, `repair_clock_inverters` keeps CTS from buffering inverted clocks, and antenna repair fixes antenna violations, which no other step repairs. Antenna repair inserts the PDK's antenna diode cells, LEF class `CORE ANTENNACELL`. A `probe` run skips the floorplan setup repair, the global-route repair and the antenna repair after global route.

## Tune global routing

The `global-route` block, on the platform or the run, sets `global_route` options; a run's block overrides the platform's field by field. Each key and the calls it reaches are in the [YAML reference](../reference/yaml.md#synthesis-and-physical-design-tools).

```yaml
global-route:
  congestion-iterations: 100
  congestion-report-iter-step: 25
  grid-origin: [3, 3]                       # DBU
  layer-adjustment: {met1: 0.9, met2: 0.8}
```

| Problem | Key |
| --- | --- |
| Overflow that more rip-up and reroute would clear | `congestion-iterations` (default 30), with `congestion-report-iter-step` to see how it falls |
| Long wires low on the stack, or one layer congested | `layer-adjustment` per layer, applied after `routing-layer-adjustment` |
| Boundary pins centred on a GCell edge fail detailed route (ASAP7 pins on a fine lattice) | `grid-origin`. OpenROAD drops the origin on every call, so rb passes it to each one, including the incremental routes of global-route and post-route repair. OpenROAD 26Q4 offsets only the guides, not the GCell grid, so rb rebuilds the grid from the guides before each detailed route. It does so after the `global_route` checkpoint, so a staged or resumed detailed route starts from the router's own route and rebuilds the grid as a one-session run does |
| Critical nets routed on resistive layers | `critical-nets-percentage`, `resistance-aware` |
| Where a design overflows, without failing at global route | `allow-congestion: true` |

`allow-congestion` is for diagnosis: global route keeps its overflow and the flow goes on to detailed route. The result is a `PASS` qualified `global-route.allow-congestion (diagnosis only)` with `allow_congestion: true` and `global_route_overflow`, never a clean pass. Such a run cannot be `harden: true`.

## Repair timing after routing

Post-CTS repair times the design on placement estimates. Two blocks, on the platform or the run, repair again on routed parasitics. When neither the platform nor the run writes `global-route-repair` or `global-route-hold-repair`, the repair after global route runs as ORFS runs it: `repair_design`, then setup and hold repair with no margin, `{repair-design: {}, setup-margin: 0, hold-margin: 0}`. `post-route-repair` runs only when set. Both cost run time and change QoR.

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

Each block runs `repair_design` when `repair-design` is set (`{}` for OpenROAD's defaults), `repair_timing -setup` when `setup-margin` is set and `repair_timing -hold` when `hold-margin` is set. At least one is required. A written block runs only what it sets. A run's block overrides the platform's field by field, and `null` turns a repair or the whole block off; `global-route-repair: null` runs no repair after global route. `global-route-hold-repair: true` is `global-route-repair: {hold-margin: 0}`.

**`global-route-repair`** runs after global route on its parasitic estimate, then legalizes and reroutes the changed nets (`reroute: incremental`, default) or the whole design (`full`).

**`post-route-repair`** adds the stage `post_route_repair` after detailed route. It needs the PDK's `rcx-rules`. It extracts and times the detailed route (round 0), then runs up to `rounds` (default 1) repair rounds, each in fresh OpenROAD sessions: repair on the extracted parasitics, route again, repair antennas as after the detailed route (with `detailed-route-antenna-repair`), extract and time. `reroute: eco` (default) keeps the detailed route and reroutes only the changed nets; `full` routes the whole design again, costing about a global and a detailed route per round. Round k repairs with the margins plus (k − 1) × `margin-step`. `rounds` above 1 needs a margin.

Each round, the electrical round included, starts by running `remove_fillers`, which removes every instance of a `CORE_SPACER` master that is not `LOCKED`. A spacer a hook placed `FIRM` or `FIXED` to reserve sites is gone after the first round; place it `LOCKED` to keep it.

Two `repair-design` keys apply to `post-route-repair` only. Both are off by default:

```yaml
post-route-repair:
  repair-design:
    slew-margin: 20
    wire-rc: {resistance: 0.06, capacitance: 0.17}   # kohm/um, fF/um; or {layer: M5}
    after-timing: true
  setup-margin: 0.05
```

- `wire-rc`: each `repair_design` estimates the nets it changes at this signal wire RC instead of the session's, which is set for placement. Routed nets that `repair_design` buffers are often longer and higher in the stack than that estimate assumes, so it can leave them over their slew limit. `{layer: M5}` takes that layer's RC per corner from `set_layer_rc`, else from the LEF. The session's signal wire RC, including one a `session` hook set, is restored right after, and `repair_timing` estimates the nets it changes at it. The nets `repair_design` changed keep their `wire-rc` estimate through `repair_timing` until OpenROAD estimates them again, for example after legalizing moves their cells. Clock wire RC is not changed. Each repair line in `pnr.log` names the signal wire RC it ran at.
- `after-timing: true`: each round runs `repair_design` again after setup and hold repair, before legalizing. `repair_timing` checks the slew of the nets it changes at the session's RC, and some end a few picoseconds over their limit. It needs `setup-margin` or `hold-margin`.

The stage keeps the best round, never worse than the detailed route:

- A clean route always beats one with DRCs.
- Otherwise a round wins when no repaired side's worst slack (setup if `setup-margin` is set, hold if `hold-margin` is set; 0 when met) is lower and one is higher, without more DRCs; or when it ties on timing with fewer DRCs.
- When a round is better than the best on one repaired side and worse on the other, without more DRCs, it wins when its worst violation, the lowest worst slack over the repaired sides, is smaller. On equal worst violations the best stays. So a round that clears a large hold miss and leaves a small setup miss becomes the best, and the next round starts from it.
- With [`signoff:`](#gate-signoff) on a multi-corner platform, each side's worst slack here, and in the stop conditions below, is taken over its signoff corners only. Each `pnr.post_route_round` event adds that judged slack as `signoff_timing`, and `pnr.post_route_repair_done` names the corners in `judged_corners`.
- When the rounds count electrical violators (with `electrical-repair` set or an electrical gate), a round that ties the best on every repaired side's worst slack and on DRCs wins when it has fewer max-slew plus max-capacitance violators. The stop reason names that tie-break. On a run that gates on electrical violators (`fail-on-electrical: true`, or `electrical` in `signoff.fail-on`), the stop reason also names the kept round's violators.

Rounds stop (`post_route_repair.stop`) when the best round is clean and meets timing (`timing_met`), a clean round does not improve (`no_improvement`), timing is not reported (`timing_not_reported`), a session fails (`session_failed`) or `rounds` run out (`last_round`). `timing_met` and `no_improvement` need a clean route, so rounds with DRCs keep going. The first clean round that does not become the best ends the stage, so no later round starts from it and raising `rounds` does not change that. A failure extracting round 0 fails the run; a failed session of a later round does not: rb warns `pnr.post_route_round_failed`, keeps the best round and qualifies the `PASS`. `finish` continues from the kept round, whose DRC report becomes `route.drc.rpt`. `pnr.post_route_round` logs each round's slack and DRCs, and names the comparison with the best round that decided whether it became the best in `decided_by`: `drc_violations`, `timing`, `worst_violation`, `electrical_violators`, or `tie` when the best stays. When the worst violation made the kept round the best, or a clean round that trades sides with the best stopped the rounds, the stop reason gives both rounds' worst violations.

### Repair electrical violators after routing

A round's reroute can leave nets over their max-slew or max-capacitance limit. `electrical-repair` adds one round after the others:

```yaml
post-route-repair:
  repair-design: {slew-margin: 20, cap-margin: 20}
  setup-margin: 0.05
  electrical-repair: {slew-margin: 10, cap-margin: 10}   # percent; {} for OpenROAD's defaults
```

- When the best round has max-slew or max-capacitance violators, the electrical round runs `repair_design` alone at these margins on that round's database and SPEF, at `repair-design.wire-rc` when that is set. It then legalizes, reroutes per `reroute`, extracts and times like any round, and becomes the kept round only if it [beats the best](#repair-timing-after-routing): better timing, fewer DRCs, a smaller worst violation when it trades sides, or a tie on timing and DRCs with fewer electrical violators.
- When the best round has no such violators, the round is skipped and `pnr.log` says so.
- Its number is `rounds` + 1. `post_route_repair.electrical_round` gives the round it repaired (`source_round`), its `status` (`kept`, `not_kept`, `skipped` or `failed`) and `reason`. A failed session is set aside like a round's.
- `repair_design` finds violators on the SPEF but sizes the buffers that repair them on the placement estimate at the round's signal wire RC, so it leaves a net whose estimate is within its limit less the margin, however far over the extraction is (see [Known Issues](../known-issues.md#post-route-repair_design-sizes-its-buffering-on-the-placement-estimate)). When the round is kept, its extraction lists each violator still on its SPEF that the estimate judges within the limit less the margin, at the scene where its extracted violation is worst. It leaves out ports and pins on constant-driven, clock, dont_touch, special or abutment nets, which `repair_design` does not repair on any parasitics. rb warns `pnr.electrical_round_unseen` naming them, and `electrical_round.unseen` lists each one's `check` (`slew` or `capacitance`), `pin`, `limit`, `extracted` and `estimated` value. Raise `repair-design.wire-rc` toward the extracted RC, or bound placement's wire length with `placement.repair-design.max-wire-length`.

With `electrical-repair` set or an electrical gate, each `pnr.post_route_round` event in `progress.jsonl` carries `max_slew_violation_count` and `max_capacitance_violation_count` over every corner and, on a multi-corner platform, each corner's in `corners`. Without either, the rounds count no violators and their scripts are unchanged.

## Probe a floorplan

`mode: probe` on a run checks quickly whether a floorplan places and routes. It skips setup and hold repair and the antenna repair after global route, stops after global route and writes the `global_route` checkpoint. The result is a `PASS` with `routed: false`, the global-route overflow and timing on the global-route estimate. `xfail:` does not apply to it, and a `closure` run cannot resume from a probe's checkpoints. A probe publishes no abstract, so `harden: true` with `mode: probe` fails to load.

## Shape the floorplan

The die is sized from the netlist's cell area by `floorplan.utilization`, which has no default, `aspect` (default 1.0) and `core-margin` (default 1.0 µm). To fix it exactly, set `die-area` and `core-area` together (`[x0, y0, x1, y1]` in µm); they exclude the other three. `core-cutouts` carves rectangles out of the core for an L- or T-shaped core: they block cells, rows, taps and macros, not routing, and the die stays rectangular. With utilization sizing, lower `utilization` to leave room for cut-outs.

`floorplan.blockages` keep standard cells out of rectangles (die µm):

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

After macro placement, and again at the end of the floorplan when an `after-pdn`, `after-pins` or `stage-end` hook is set, a macro that overlaps a `hard` blockage or a core cut-out fails the run, naming the macro, the region and the ways out: a `floorplan.macros` location, a larger `placement.macro-channel`, or moving the blockage. RTL-MP treats every blockage as an obstacle and stops with `MPL-0065` or `MPL-0040` when it finds no placement clear of them, but rb checks only `hard` blockages and cut-outs. `lifetime: global-placement` makes any type a screen for global placement alone: no rows are cut, macro placement ignores it and no checkpoint holds it. Blockages need OpenROAD 26Q1.

## Place macros

A netlist with hard macros (SRAMs, hardened blocks) gets them placed before the power grid by OpenROAD's RTL-MP (`rtl_macro_placer`), as ORFS places them. RTL-MP places by connectivity and may mirror a macro. It reports to `rtlmp/` in the run directory, and `pnr.log` has one `placed` line per macro it places. rb passes it the arguments ORFS does: `-target_util` is `placement.density`, and `placement.macro-channel` is its minimum channel in µm between macros and from the core edge (`-min_channel_size`), which it trims to the macro pins along each channel unless `placement.macro-pin-aware-channels` is `false`. ORFS sets a channel on every platform, such as `[80, 80]` on sky130hd; rb sets none unless configured, and RTL-MP then keeps its own channels, which can leave none. A channel too narrow for the power grid fails `pdngen` with `PDN-0179`, and one too narrow for routing fails global routing with `GRT-0116`; set `placement.macro-channel` then. It needs OpenROAD 26Q4 or newer; on an older one, a run with macros stops at macro placement saying so. To place macros another way, fix them with `floorplan.macros` locations, or place them in a [`macro-placement` hook](#run-tcl-hooks), which replaces RTL-MP. Whichever places them, no macro may overlap a core cut-out or a `hard` [blockage](#shape-the-floorplan); the run fails otherwise.

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
```

- A `location` is snapped to the site and row grid, must be inside the core and clear of other fixed macros, and must match one macro. RTL-MP places the other macros around it, and does not run when every macro is located.
- An `orientation` needs a `location`, because RTL-MP chooses the orientation of each macro it places. With a `macro-placement` hook, an entry may orient a macro for the hook to place.
- A pattern that matches nothing fails the run.
- A turned macro (`R90`, `R270`, `MXR90`, `MYR90`) turns its pins and power pins. Give the `pdn-config` a macro grid for that orientation, or `pdngen` fails (`PDN-0233`). `placement.rotated-macro-footprint` works around OpenROAD's legalizer for turned macros; see [Known Issues](../known-issues.md#openroads-legalizer-blocks-a-rotated-macros-unrotated-footprint).

A floorplanner can hand over macros as a YAML or JSON list of the same entries in `floorplan.macros-file`. `macro-snap: false` keeps its exact coordinates.

## Place IO pins

`floorplan.pins` constrains IO pins by port name or glob:

```yaml
    floorplan:
      pins:
        - {names: [req_*, clk], side: left}
        - {names: rsp_*, side: right, start: 10, end: 80}   # die µm along the edge
        - {names: pwdata, side: top, group: true, order: true}
        - {names: irq, location: [0, 42.5], layer: met3, size: [0.3, 0.6]}
```

- A `location` entry places one pin exactly, moved onto the nearest die edge and routing track. `pin-snap: false` keeps its centre where given.
- Exact pins are placed first; side and group rules skip them. When every signal port has an exact pin, `place_pins` is skipped.
- A name that matches no port fails the run.
- `pins-file` takes a planner's YAML or JSON list of exact pins and `floorplan.pins` entries.
- `pins-complete: true` fails the run (`fail_stage: pins_complete`) when a signal port has no matching entry, more than one exact pin, or no exact pin and several side or group rules.

For other pin Tcl, such as `-mirrored_pins` or `exclude_io_pin_region`, set `pin-constraints: pins.tcl` on the run. It is sourced just before pin placement. Do not constrain a port that has an exact pin there (`PPL-0107`), and do not put pin commands in the SDC, which is read before the die exists.

After routing, every located macro and exact pin is compared with the routed DEF; a moved one fails the run with `fail_stage: routed_check`.

## Plan the power grid

The PDK's `pdn-config` declares the grid for every run. A run can replace it:

- `pdn-config:` on the run sources another Tcl file instead.
- `pdn:` on the run declares the core grid in YAML and replaces the core grid of the pdn-config in effect, keeping its global connections, voltage domains and macro grids. It still needs a pdn-config.

```yaml
    pdn:
      ring: {layers: [met5, met4], width: 1.6, spacing: 1.7, offset: 2}
      stripes:
        - {layer: met1, width: 0.48, followpins: true}
        - {layer: met4, width: 1.6, pitch: 20, offset: 5}
        - {layer: met5, width: 1.6, pitch: 20, offset: 5}
```

By default each stripe layer connects to the next; `connect: [[met1, met4], ...]` lists the pairs instead. A ring must fit between core and die. Only one always-on power domain is supported. The PDK's global connections are applied again at the end of every stage, so cells added after `pdngen` get supply connections.

A macro grid in the `pdn-config` with a `-halo` needs the standard-cell rows cut at least that far around each macro: `pdngen` fails with `PDN-0008` when a halo reaches a row the macro does not sit on. The tap-cell step cuts them, after macro placement and before the grid. Without a `tapcell-tcl` it runs `cut_rows`, which cuts 2 µm around each placed macro, as ORFS does, enough for ORFS's 2 µm macro-grid halos. For a wider halo, cut the rows in the PDK's `tapcell-tcl`, for example `cut_rows -halo_width_x 5 -halo_width_y 5` before `tapcell`. Shrinking the halo instead can leave the core straps ending next to the macro, where they block its pin escapes.

## Run Tcl hooks

`hooks:` on a run sources your Tcl files at fixed points of the flow, for steps rb does not have. Each point takes a path or a list, relative to `pnr.yaml`:

```yaml
    hooks:
      session: [wire_rc.tcl]
      after-cts: [io_latency.tcl]
```

| Point | Sourced |
| --- | --- |
| `session` | At the start of every OpenROAD session, after the SDC, before the floorplan. Also in a [`netlist-source: pnr` power run](power.md#extracted-parasitics) of the run |
| `after-floorplan` | After floorplan, tracks, blockages, tie cells and the floorplan setup repair |
| `macro-placement` | Instead of RTL-MP; only when the netlist has macros. `$rb_movable_macros` lists the unlocated ones. |
| `after-pdn` | After taps and the power grid |
| `after-pins` | After pin placement, before port buffering. A hook that buffers ports needs `buffer-ports: false`. |
| `after-global-placement` | After `global_placement` |
| `after-repair-design` | After `repair_design`, before legalization and the `placement.macro-pin-limits` step |
| `before-cts` | After legalization, `improve_placement`, `optimize_mirroring` and the `placement.macro-pin-limits` step, before `repair_clock_inverters` and `clock_tree_synthesis`. The `cts-dont-use-cells` are not yet excluded. |
| `before-repair-clock-nets` | Right after `clock_tree_synthesis`, before `cts-repair-clock-nets` runs `repair_clock_nets`. The `cts-dont-use-cells` are still excluded. |
| `after-cts` | After CTS and any `repair_clock_nets`, before setup and hold repair |
| `after-cts-repair` | After post-CTS setup and hold repair and their legalization |
| `after-global-route` | After `global_route`, before global-route repair |
| `after-global-route-repair` | After global-route repair, before the antenna repair, in the `global_route` stage, so also in a probe |
| `before-detailed-route` | Before the flow's detailed route and each post-route repair round's; not before the antenna repair's |
| `after-extraction` | In every session that writes a SPEF (each post-route repair round's extraction and the final one), after `write_spef` and before rb reads it. `$rb_spef` is its path. Needs `rcx-rules`. |
| `before-reports` | After fill and extraction, before the final reports |
| `after-reports` | After the final reports, on their parasitics, before rb writes the routed outputs |
| `stage-end` | At the end of every stage, before its checkpoint, with `$rb_stage` naming the stage and `$rb_mode` the run's `mode` (`closure` or `probe`). In post-route repair, at the end of each repair round, including the electrical round; when round 0 stops the rounds, the stage keeps the detailed route and runs none. |

Hooks run at Tcl global scope in `artefacts/<run>/`, with `$OUT_DIR` and `$DESIGN` set. A Tcl error, `exit` or stray `return` in a hook fails the run naming the point and file; a `stage-end` hook's error also names the stage, and fails the run in a post-route repair round too, where other failures only set the round aside. Results come from the final reports, so a report a hook prints before or after them does not change them. Hook files are inputs of checkpoints and hardened abstracts. On a multi-corner platform an SDC command in a hook constrains the primary corner only; see [Sign off at several corners](#sign-off-at-several-corners).

Each point belongs to the stage it runs in, `before-cts` to `cts`: a staged run runs it in that stage's session where a one-session run runs it, and `--resume` from that stage runs it again.

Every later `read_spef` of a SPEF reads what the `after-extraction` hooks left: the next post-route repair round's, the final reports' and `rb power`'s. A multi-corner platform writes one SPEF and reads it once per corner, so one edit covers every corner.

rb writes into `$OUT_DIR` and replaces a file of the same name a hook wrote there: `timing.rpt` and `electrical.rpt` in the final reports, then, after the `after-reports` hooks, `<top>.def`, `<top>.routed.v`, `<top>.routed.pwr.v`, `<top>.routed.sdc`, `<top>.<corner>.routed.sdc` and `<top>.routed.odb`, and with `harden: true` the `abstract/` views. After OpenROAD exits, rb writes `worst_paths.rpt` and `fanout.rpt`. The other names are in [Inspect artefacts](#inspect-artefacts). Every run first deletes the files ending in `.def`, `.routed.v`, `.routed.pwr.v`, `.routed.sdc`, `.routed.odb`, `.routed.spef`, `.gds` or `.png`, so give a hook's own files other names.

rb does not read the Tcl for `source` targets. List the files the SDC or a hook sources under `inputs:`, relative to `pnr.yaml`. Each is fingerprinted by path and content with the hook files, so editing one or listing another in its place refuses a `--resume` and makes a hardened block's abstract stale. `rb pnr` fails before any run starts when a run it would run lists a missing file:

```yaml
    inputs: [scripts/window.tcl]
```

When a run splits into several OpenROAD sessions (`stages: true`, `--resume`, `--dispatch slurm`, or `post-route-repair`), Tcl procedures, `rename` wrappers, `set_wire_rc` and `set_dont_use` set by a hook are gone in the next session. Put such state in a `session` hook. rb warns `pnr.hook_commands_not_kept` when a hook redefines an existing command.

## OpenROAD threads

Unset, `threads:` passes nothing and OpenROAD uses its own default: every hardware thread of the host on 26Q4, one thread on older versions. Set `threads:` to a count or `auto` to choose. `auto` takes the CPUs of the Slurm allocation or affinity mask the run is in, 1 outside one, never the host's core count. A count above the allocation is clamped with `openroad.threads_capped`. Run `rb pnr` inside an allocation (`srun -c 8 rb pnr ...`) to give it CPUs, or use [`--dispatch slurm`](#dispatch-the-stages-to-slurm). Results can differ slightly between thread counts. The result's `openroad_threads` gives `requested` and `effective`, the count OpenROAD logged using, or the count rb set. With `threads:` unset OpenROAD logs no count, so `effective` is null, as is the `threads` that a staged run records in each stage's `stage_end` event and manifest entry. A one-session run records no per-stage `threads`. `rb power` and `rb synth`'s OpenROAD stage read `threads:` and report `openroad_threads` the same way.

## Keep stage checkpoints

A run killed at a wall-time limit leaves only its log. `checkpoints: true` (or one stage, or a list) writes `<NN>_<stage>.odb`, `.def` and `.sdc` at each stage boundary into `checkpoints/<run-id>/`, with `checkpoints/latest` pointing at the newest:

| Stage | Holds |
| --- | --- |
| `floorplan` | Floorplan, macros, power grid, pins |
| `place` | Legalized global placement |
| `cts` | Clock tree and hold repair |
| `global_route` | The global route after global-route repair and antenna repair, with route guides and segments and `grt_antennas.rpt`; with `global-route.grid-origin`, before the GCell grid is rebuilt from the guides |
| `detailed_route` | The detailed route after antenna repair, before fill, with its DRC report and `drt_antennas.rpt` |
| `post_route_repair` | The kept repair round, with its DRC report and `drt_antennas.rpt` (only with `post-route-repair`) |

On a multi-corner platform the `.sdc` holds every corner's SDC mode, and `<NN>_<stage>.<corner>.sdc` each corner's alone.

Look at a checkpoint in OpenROAD or with `rb pnr-export --checkpoint`. It is never a final result. Old run directories are not pruned.

A run that fails in global route keeps that route's database as the `global_route` checkpoint, marked `failed`, with a copy of each congestion report (`04_global_route.congestion.rpt`, `04_global_route.congestion-<i>.rpt`), whether or not the run sets `checkpoints`; a run without them gets a run directory for it. The run still fails, and its `failed_global_route` field names the checkpoint as `<run-id>/global_route`. `rb pnr-export --checkpoint` opens it; no `--resume` starts from it, and `checkpoint_stages` does not list it. A failure a hook catches is dropped when a later global route passes or the `global_route` stage ends. A database OpenROAD did not finish writing, or that rb could not file, stays in `global_route_failed/` until the next run, and the `pnr.failed_global_route_not_kept` warning names it.

## Compare timing across stages

A run that writes `progress.jsonl` (`checkpoints:` set, even to `[]`, `stages: true`, `--stop-after`, or post-route repair) appends one `stage_timing` event to it when each stage ends, and lists the events per stage under `timing` in `manifest.json`. They compare iterations of a block stage by stage without reports of your own:

```json
{"event": "stage_timing", "stage": "global_route", "index": "04", "point": "stage_end", "parasitics": "global_route", "corners": {"tt": {"wns_setup_ps": -224.675, "wns_hold_ps": 343.534, "tns_ps": -322.268, "tns_hold_ps": 0.0}}}
```

| Field | Meaning |
| --- | --- |
| `stage`, `index` | The stage and its file index. |
| `point` | `stage_end`, `before_repair` or `round`. |
| `round` | The post-route repair round (0 is the detailed route); on a `stage_end` of `post_route_repair`, the kept round. |
| `parasitics` | What the slacks were timed on: `none` (no wire RC), `placement` (placement estimate), `global_route` (global-route estimate) or `extracted` (OpenRCX SPEF). |
| `corners` | Per corner, in config order, `wns_setup_ps`, `wns_hold_ps`, `tns_ps` and `tns_hold_ps`, in picoseconds as in the result row. A single-corner platform has one entry, named by its `corner:`. |

| Stage | Events | `parasitics` |
| --- | --- | --- |
| `floorplan` | `stage_end` | `none` |
| `place`, `cts` | `stage_end` | `placement` |
| `global_route` | `before_repair` when [global-route repair](#tune-placement-clock-tree-and-repair) runs, then `stage_end` after it | `global_route` |
| `detailed_route` | `stage_end` | `global_route`, the estimate from the route guides |
| `post_route_repair` | one `round` per round, then a `stage_end` copied from the kept round | `extracted` |
| `finish` | `stage_end` | `extracted` with `rcx-rules`, else `global_route` |

A stage's numbers are what its own report prints on the same parasitics: the `--stop-after` stage report, and the final reports for `finish`. They carry more digits than the report. A side with no constrained path has null WNS and TNS. A corner rb could not time has nulls and an `error`; this never fails the run, and no result field or verdict reads the events.

## Run stages as separate sessions

`stages: true` runs each stage (`floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `post_route_repair`, `finish`) as its own OpenROAD session that reads the previous stage's checkpoint. It writes every checkpoint, keeps each session's script and log in the checkpoint directory, and concatenates them into `pnr.tcl` and `pnr.log`. A staged run gives the same result as a one-session run at the same thread count, with the exceptions in [Known Issues](../known-issues.md#a-staged-pr-run-can-differ-from-a-one-session-run).

Stop early or resume:

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --stop-after global_route
rb pnr alu_pnr -c pnr/alu/pnr.yaml --resume latest --from detailed_route
```

- `--stop-after <stage>` works on any run, writes that checkpoint and a stage report, and passes with `routed: false`. It writes no routed outputs, GDS or abstract, and `xfail:` does not apply.
- `--resume <run> --from <stage>` starts a new staged run from a staged run's checkpoint of the stage before. `<run>` is `latest`, a run id or a checkpoint directory. The old run is left unchanged, so detailed routing can be retried from one global route. The reports the inherited checkpoints keep (`grt_antennas.rpt`, `route.drc.rpt`, `route.maze.log`, `drt_antennas.rpt`) are copied into `artefacts/<run>/`.
- The resume is refused (`fail_stage: resume`) when the earlier stages' generated scripts, their input files or the OpenROAD version changed, or when resuming a probe into a closure run. A `constraints:` path and a map of that path to every corner count as different. Settings read only from `--from` on, such as `detailed-route-verbose`, routing layers or `threads:`, may change. Files sourced by hooks, `platform-tcl`, a `pdn-config` or the SDC are not checked unless listed under [`inputs:`](#run-tcl-hooks), and environment variables are not checked.

## Dispatch the stages to Slurm

`--dispatch slurm` runs each stage as its own Slurm job, each held until the previous one succeeds, and implies `stages: true`. `rb pnr` stays on the submitting host, waits for the chain, then checks the result and streams out GDS there, so give that host memory for KLayout.

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

Each job's reservation comes from `resources.stages.<stage>`, then the run's `resources:`, then `cfg-dispatch.resources`, then built-in defaults (a run with no `resources:` warns `pnr.dispatch_resources_default`). With `threads: auto` each stage uses its job's CPUs. Jobs are named `rb:pnr:<run>:<stage>`; `cfg-dispatch.sbatch-args` apply to each. `--dispatch local` (default) runs stages in place.

- A job that fails, times out or runs out of memory fails the run at that stage (`fail_stage: dispatch` unless the stage itself failed) and Slurm removes the jobs after it.
- A stage job sbatch refuses because the cluster's queue is full (`MaxJobCount`) is resubmitted with backoff for up to 30 min (`dispatch.submit_deferred`; see [Dispatch](dispatch.md#troubleshoot-reservations-arrays-and-waits)).
- Ctrl-C, SIGTERM or SIGHUP cancels the run's jobs.
- If `rb pnr` itself is killed, its jobs keep running. Until they leave the queue, a new `rb pnr` of the same configuration refuses to start and prints the `scancel` command. On a host without `squeue`, that refusal lapses once the dispatch directory has been quiet for the sum of the stages' time limits (`pnr.stage_jobs_unchecked`), so check the queue before starting again. See [Known Issues](../known-issues.md#an-unconfirmed-dispatched-pr-run-blocks-later-runs).
- `checkpoints/<run-id>/dispatch/` holds each job's id, Slurm log, rtl_buddy log and result.

## Stream out GDS and PNG

`--gds` streams the routed DEF to GDS with KLayout after a successful run; `--png` also renders it. Cell layouts come from the PDK's `cell-gds` and the run's `gds-paths` (put each macro's GDS there, its LEF in `lef-paths`). A missing input stops the export.

A cell in the DEF with no layout is written as an empty placeholder. `gds-mode` decides what that means:

- `preview` (default): the layout is kept, the run passes, and `gds_status: incomplete` and `gds_missing_cells` name the cells. A KLayout failure only warns.
- `strict`: any missing cell, missing KLayout or `klayout-tech`, or export failure fails the run with `fail_stage: export` and removes the GDS and PNG. The routed outputs stay.

List cells that are empty on purpose, such as an ORFS `fakeram45_*` macro, in `gds-allow-empty`.

## Export a saved result

`rb pnr-export` streams an existing routed result out again without rerunning OpenROAD, for example after installing KLayout:

```bash
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --png --gds-mode strict
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --checkpoint cts --png
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --png-only --lyp dark.lyp
```

`--checkpoint` exports a stage checkpoint into `checkpoints/<run-id>/export/`, marked not final. `--def` exports another DEF with the run's platform. `--png-only` re-renders the existing GDS. The export never touches the routed results. Its verdict is the export alone: anything not delivered is `FAIL` with `fail_stage: export`. `export.provenance.json` records what it read and wrote.

## Harden a block

`harden: true` publishes a routed block as a hard macro for a larger design: `abstract/<top>.lef`, `<top>.lib` (timing only), `<top>.gds` and `abstract.manifest.json`, all or nothing (`fail_stage: abstract`). `<top>.lib` lists the supply pins last and is the same whether the run was staged or one session. `<top>.params.json` records the block's parameter values; it is left out, with the warning `pnr.block_params_unrecorded`, when Yosys cannot elaborate the top.

```yaml
runs:
  - name: alu_block_pnr
    # ...
    platform: sky130hd_tt_block
    harden: true
```

- It forces `--gds` with `gds-mode: strict`, so it needs KLayout and the PDK's `klayout-tech`. `abstract: {gds: false}` publishes no GDS instead, records it as `null` in the manifest and leaves stream-out to `--gds`.
- It needs a single-corner platform.
- It turns on `buffer-ports`, so each port drives or is driven by one buffer and the abstract's pin load does not depend on the block's internals. Set the platform's `port-buffer`; on sky130hd OpenROAD otherwise picks a delay cell.
- The abstract LEF blocks every layer the block uses over its whole area. `abstract: {bloat: 0}` writes the real shapes, a number grows them, and `feedthrough-layers: [met4]` writes real shapes on the listed layers only, so the parent can route across. Check the parent's DRCs after changing it.
- A parent's `rb power` sees the block as 0 W. Run `rb power` on the block itself.

**Power grid.** The parent's top straps cross the block and drop vias onto its supply pins, so the block must leave the top layer free. Give the block its own PDK copy with a `pdn-config` whose straps stop below the parent's (on sky130hd: block met1 to met4 with power pins on met4, parent straps on met5), and set the block platform's `routing-layers` to match. The template's `pdn_block.tcl` and `sky130hd_tt_block` show this. rb ties each block supply pin to the parent net of the same name, or the only power or ground net.

A block that must use the top layer, such as one with an OpenRAM sky130 SRAM, exposes its supply pins there. rb then joins each pin to the parent strap of its net on the same track. For that to work, build the block's top straps on the parent's width, pitch and net order, place it a whole number of pitches from the parent's strap offset, and define the parent's macro grids with `-cells` or `-instances` instead of `-default`. A pin it cannot join fails with `block power: <inst>/<pin> ...`.

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

- Build blocks first. `rb pnr -c top/pnr.yaml --synth` runs every block, its synthesis and the top in order from a clean tree; `-j N` hardens independent blocks in parallel. Each run uses its own `threads:`.
- A run whose block failed is not attempted (`fail_stage: blocked`, with `blocked_by`).
- The block must use the same technology LEF and corner as the top.
- An abstract whose inputs changed since hardening, or that a major version of rb with different built-in flow steps or defaults hardened, is refused, naming what changed. Re-harden it, or pass `--accept-stale`.
- A block hardened with `abstract: {gds: false}` has no GDS. A `gds-mode: strict` stream-out of the parent, including the parent's own `harden` and `rb pnr-export --gds-mode strict`, fails at setup naming the block. A `preview` stream-out, `--png` included, treats the block as a cell without GDS: the layout lacks it and the result names it, as in [Stream out GDS and PNG](#stream-out-gds-and-png).

## Instance a parameterised block

A hardened block has one parameter set, its synthesis's `params:`. A parent that writes `blk #(.W(16)) u_blk (...)` needs a stub that declares the parameters:

```systemverilog
(* blackbox *)
module blk #(parameter int W = 8) (input logic clk, input logic [W-1:0] d, output logic [W-1:0] q);
endmodule
```

Use `frontend: slang` for such a parent. rb rewrites each instance to `blk u_blk (...)` before OpenROAD reads it, after checking it against the block: every connected port must exist in the abstract with the same width, every input must be connected, and each override must equal the value recorded in `<top>.params.json`. A mismatch stops the run naming the instance and both values. Re-harden a block with no record.

## Flow reference

For maintainers and hook authors. The generated `pnr.tcl` runs:

1. `platform-tcl`, Liberty, LEF, a mode and scene per corner on a multi-corner platform, netlist, SDC, `max-fanout`, `layer-rc-tcl`, don't-use cells, `session` hooks. The SDC and `max-fanout` go into every mode.
2. Floorplan, tracks, blockages and cut-outs, tie cells, floorplan setup repair.
3. Macro placement, tap cells, power grid, pin placement, port buffering.
4. Global placement (with `lifetime: global-placement` and rotated-macro blockages present, and the output port buffers `hold-outputs-at-pins` fixed), freeing those buffers, `repair_tie_fanout`, `repair_design`, legalization, `improve_placement` and `optimize_mirroring`, the `placement.macro-pin-limits` step.
5. `repair_clock_inverters`, CTS, post-CTS setup repair, hold repair, legalization.
6. Global route, global-route repair, antenna repair, detailed route, antenna repair with detailed routes, optional post-route repair stage, whose rounds each repair antennas after their detailed route.
7. Fill, global connections, extraction with `rcx-rules`, final reports, outputs.
8. After OpenROAD: the routed-floorplan check, then stream-out and abstract.

The `stage-end` hooks run at the end of each stage, after its last global connection; the stage's checkpoint is written after them.

A `probe` skips the setup and hold repairs and the antenna repair after global route, and stops after global route. Each session must print its end marker (`>>> DONE`, `>>> End of stage <NN>_<stage>`, `>>> STOPPED after <stage>`, or the post-route round markers), or the run fails `incomplete`. This check comes before any check of the design.

A staged session starts with a prelude regenerated from the same template: the header, Liberty and LEF, `read_db` and `read_sdc` of the previous checkpoint (every mode), its saved flow variables, then step 1's per-session settings. It keeps only what the database, the checkpoint SDC and the state file hold. A resume compares each earlier stage's script up to its end-of-stage line, ignoring comments, the run-directory lines, `set_thread_count` and header variables the stage does not read.

Each stage's wall time, CPU time and peak memory are recorded as `stage_end` events in `progress.jsonl` and under `resources` in the manifest. In a one-session run the peak memory of a stage includes what earlier stages left resident. Its [timing](#compare-timing-across-stages) is recorded as `stage_timing` events and under `timing`.
