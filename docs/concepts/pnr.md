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

Outputs go to `artefacts/<run>/` next to `pnr.yaml`; each run deletes the previous run's outputs first. With no run name, `rb pnr` runs every entry whose `reglvl` is within `-l`, hardened blocks before the runs that use them.

## Interpret results

`PASS` means OpenROAD ran the flow to its end and wrote its outputs. It does not mean the design meets timing or routes clean. Judge the design on these fields:

| Field | Meaning |
| --- | --- |
| `wns_setup_ps`, `wns_hold_ps` | Worst setup and hold slack. Negative misses timing. |
| `tns_ps`, `tns_hold_ps` | Total negative setup and hold slack. |
| `drc_count` | Violations left in `route.drc.rpt`. 0 is a clean route. |
| `max_slew_violation_count`, `max_capacitance_violation_count`, `max_fanout_violation_count` | Pins over a Liberty or SDC limit at any corner, listed in `electrical.rpt`. |
| `area_um2` | Design area. |
| `routed_cell_count` | Instances of the finished design, including added tie, buffer and clock cells, excluding physical-only cells. Compare experiments on this. |
| `cell_count` | Instances of the input netlist (the summary's Cells column). |
| `physical_cell_count` | Tap, endcap, spacer and fill cells. |
| `worst_setup_corner`, `worst_hold_corner`, `corners` | Multi-corner platforms: the corner behind each WNS, and each corner's four timing values. |
| `routed`, `mode`, `stopped_after`, `global_route_overflow` | `routed: false` means the run stopped early ([probe](#probe-a-floorplan) or `--stop-after`); its numbers are that stage's estimate, with the global-route overflow from `global_route` on. |
| `post_route_repair` | The rounds of [post-route repair](#repair-timing-after-routing): `kept_round`, `rounds`, `repaired_rounds`, `stop`, `reason`, `passed_over_round`. |
| `openroad_threads` | Requested and effective [thread counts](#openroad-threads). |
| `gds_status`, `gds_missing_cells` | [Stream-out](#stream-out-gds-and-png) outcome. |
| `checkpoint_dir`, `checkpoint_stages`, `last_step` | With [checkpoints](#keep-stage-checkpoints): where they are, which stages were written, and the last step reached. |

Times are in picoseconds, converted from the Liberty `time_unit`. The final timing uses extracted parasitics when the PDK sets `rcx-rules`, else the global-route estimate. A field rb could not read is left out.

A passing run's description carries qualifiers worth reading:

- `pnr.no_wire_rc`: placement and CTS ran without wire RC. See [Set wire RC](#set-wire-rc).
- `electrical N max-slew, ... violator(s)`: set `fail-on-electrical: true` to fail such runs.
- `post-route repair ...`: a repair round was passed over or its session failed and was set aside.
- `GDS incomplete: ...`: cells with no layout in `preview` mode.
- `stale block abstract(s) accepted: ...`: run with `--accept-stale`.

A run fails when OpenROAD exits non-zero, logs an `[ERROR ...]` line, stops before the end of its flow, or a check below fails. It is `SKIP` when `-l` filters it out or `tool:` is not `openroad`.

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
| `route.drc.rpt`, `route.maze.log` | Detailed-route DRCs and router log |
| `congestion.rpt` | Overflowing global-route tiles, only when global routing overflows. Loads in the OpenROAD GUI's DRC viewer. |
| `<top>.gds`, `<top>.png`, `klayout.*.log`, `def2stream.inputs.json` | Stream-out outputs, logs and the inputs it read |
| `checkpoints/`, `abstract/` | [Stage checkpoints](#keep-stage-checkpoints) and a [hardened block's](#harden-a-block) views |

When OpenROAD fails, the run removes the routed database so `rb power` never reads a stale one; `pnr.log`, `pnr.tcl` and `congestion.rpt` stay. A run that fails a later check (`routed_check`, `fail-on-electrical`, `export`, `abstract`) keeps its routed outputs for inspection.

## Follow a running flow

- `tail -f artefacts/<run>/pnr.log`. Each step starts with a `>>> ` line.
- Global route prints a per-layer usage table (`GRT-0096 Final congestion report`).
- Detailed route logs each iteration (`DRT-0195`) and its violation count (`DRT-0199`), so a converging route can be told from a stalled one. `detailed-route-verbose: 0` silences this; higher levels add detail.
- With [checkpoints](#keep-stage-checkpoints), `checkpoints/latest/progress.jsonl` records each step, hook and stage. After a kill, the last `step_begin` without a `step_end` is where the run was.

## Fix a failed run

Read the row's description and `fail_stage` first, then `pnr.log`. A result with any `fail_stage` failed before or instead of a verdict on the design, so `xfail:` never excuses it. A failure without `fail_stage` (an OpenROAD error, a don't-use violation, `fail-on-electrical`) can be excused.

| Symptom | Cause and fix |
| --- | --- |
| `fail_stage: setup` | Configuration or input problem found before OpenROAD started: a missing file (`pdn-config`, `rcx-rules`, a hook, a planner file), a bad planner-file entry, `post-route-repair` without `rcx-rules`, blockages on OpenROAD older than 26Q1, a per-corner `constraints:` that does not fit the platform, or an OpenROAD that does not answer the [SDC mode check](#sign-off-at-several-corners). The description names it. |
| `fail_stage: incomplete`, `the flow did not finish` | A Tcl file the flow sources (PDK Tcl, `pdn-config`, `pin-constraints`, a hook) called `exit` or `return -level 2`, ending OpenROAD with code 0. The description names the last step reached; remove the exit from the file that step sources. |
| `fail_stage: routed_check` | A located macro or exact pin moved between floorplan and the routed DEF, usually because a `pin-constraints` file or hook moved it. |
| `fail_stage: pins_complete` | `pins-complete: true` found a port with no pin entry, or with conflicting entries. Add or remove entries. |
| `fail_stage: checkpoint` | A checkpoint could not be written (disk full; see the `checkpoint` event in `progress.jsonl`), or a hook wrapped `global_placement`, `clock_tree_synthesis`, `global_route` or `detailed_route` with `rename` in a one-session run. Free space, or remove the wrapper or set `stages: true`. |
| `fail_stage: resume` | `--resume` refused: a stage script, input file, checkpoint or OpenROAD version differs. Rerun from an earlier stage. |
| `fail_stage: dispatch` | A Slurm stage job ended `OUT_OF_MEMORY`, `TIMEOUT`, `CANCELLED` or similar, never started, or was refused. Read `dispatch/<NN>_<stage>.slurm.log`; raise that stage's `resources:`. See [Dispatch](#dispatch-the-stages-to-slurm). |
| `fail_stage: abstract` | A hardened block's view could not be written. Read `pnr.log`. |
| `fail_stage: export` | Stream-out failed under `gds-mode: strict`: KLayout, `klayout-tech`, an input file or the render. Fix it and run `rb pnr-export`. |
| `fail_stage: blocked`, `synth`, `error` | A block this run uses failed, its upstream synthesis failed under `--synth`, or rb crashed (the exception is in the description). Fix the cause first. |
| `[ERROR PDN-0179] Unable to repair all channels` | Macros too close for the power grid. Raise `placement.macro-halo` (about 19 µm minimum on sky130hd). |
| `PDN-0008 ... halo overlaps row` | A macro grid's `-halo` in `pdn-config` reaches into a row. Reduce it to the value the message suggests. |
| `PDN-0233` on a turned macro or a block's grid | The power grid has no via for that macro. See [Place macros](#place-macros) and [Harden a block](#harden-a-block). |
| `N macros do not fit the W x H um floorplan core` | Lower `utilization`, change `aspect`, or lower `placement.macro-halo`. The message gives the smallest core that fits. |
| `GRT-0116`, global-route overflow | Too congested. See `congestion.rpt`; lower utilization or density, or try `placement.routability-driven`. |
| Large `drc_count` | Try `routing-layer-adjustment`, a lower density, or `post-route-repair` with several rounds. |
| `RSZ-0090` at a slow corner | A macro with one Liberty limits that corner. Give it a Liberty per corner. |
| `RSZ-0074` in a post-route repair round | Set `post-route-repair.reroute: full`. |
| `CTS-0080 Sink not found` | Set `cts-sink-clustering: false`. |
| `GRT-0273 Disabled NDR`, large hold violations | Clock nets with non-default rules detour. Set `cts-apply-ndr: none`. |
| `DPL-0011` | A macro placed off the row grid (`macro-snap: false`). Put its bottom edge on a row. |
| `IFP-0039` at `make_tracks` (ASAP7) | Set the PDK's `tracks-tcl`. |
| `RB-DONT-USE-VIOLATION` | The routed design uses an excluded cell, usually one the netlist already had. Change the RTL, synthesis or pattern. |
| `STA-0122 cell '<pattern>' not found` | A `dont-use-cells` pattern matches nothing. |
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
| `tie-hi`, `tie-lo`, `fill-cells`, `pin-layers` | Tie cells, filler masters, horizontal and vertical pin layers |
| `cell-gds`, `klayout-tech`, `klayout-props` | Stream-out layout and KLayout technology and colours |
| `pdn-config` | Tcl that declares the power grid (no `pdngen` call). Unset means no grid. |
| `rcx-rules` | OpenRCX rules. Enables extraction, `<top>.routed.spef` and post-route repair. Its `LayerCount` must match the LEF. |
| `layer-rc-tcl` | `set_layer_rc` / `set_wire_rc` lines. Sourced after `read_sdc`. See [Set wire RC](#set-wire-rc). |
| `platform-tcl` | Sourced before the first Liberty read, such as `suppress_message` lines |
| `tracks-tcl` | Replaces the bare `make_tracks` after floorplan initialisation |
| `tapcell-tcl` | Inserts tap and endcap cells, after macro placement and before the power grid |
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

A macro with a single Liberty keeps that file's timing at every corner and can fail a slow corner (`RSZ-0090`). Give it a Liberty per corner. A multi-corner platform cannot harden a block or consume hardened blocks.

## Set wire RC

Set the PDK's `layer-rc-tcl`. Without it, placement, clock-tree synthesis and hold repair see the technology LEF's layer RC, which is zero on most open PDKs, and the routed design can fail hold that repair never saw. rb warns `pnr.no_wire_rc` and qualifies the `PASS` when the PDK sets no `layer-rc-tcl` or `pnr.log` has `EST-0018` (zero wire capacitance) or `CTS-0104` (zero clock wire RC).

For sky130hd, copy the `set_layer_rc` lines of OpenROAD's `test/sky130hd/sky130hd.rc` and pick the wire layers:

```tcl
set_layer_rc -layer met1 -capacitance 1.72375E-04 -resistance 8.929e-04
# ... one line per layer and via
set_wire_rc -signal -layer met2
set_wire_rc -clock -layer met5
```

To catch hold problems from wire length before detailed routing, also set `global-route-hold-repair: true` on the platform. See [Repair timing after routing](#repair-timing-after-routing).

## PDK setup notes

- **Nangate45.** All defaults are tuned for it. It has no `pdn-config`, so runs have no power grid. For extraction use ORFS `flow/platforms/nangate45/rcx_patterns.rules`.
- **sky130hd.** The template's `sky130hd` entry is a worked example: `pin-layers` `met3` / `met2`, `met*` routing layers, a `pdn-config`, `placement.density: 0.60`, a `dont-use-cells` list for the probe and `lpflow` cells, a `cts-buffer` list, and ORFS `sky130hs/rcx_patterns.rules`. Set `port-buffer` for hardened blocks.
- **ASAP7.** Set the four Tcl keys to ORFS's `liberty_suppressions.tcl`, `setRC.tcl`, `openRoad/make_tracks.tcl` and a copy of `openRoad/tapcell.tcl` with `TAP_CELL_NAME` and `MACRO_ROWS_HALO_X` / `_Y` filled in. List each corner's split Liberty files under that corner and extra LEFs in `lef-paths`. ORFS uses `routing-layer-adjustment: 0.25`.

## Tune placement, clock tree and repair

Process-dependent knobs live on the PDK, can be overridden on the platform, and the `placement.*` ones also on the run, field by field. Defaults suit Nangate45. Start from the problem:

| Problem | Knob |
| --- | --- |
| Global route overflows | Lower `placement.density` (default 0.7) or `floorplan.utilization`; `placement.routability-driven: true` (add `routability-use-grt: true` for a slower, closer estimate); a fixed `placement.reference-hpwl` such as `446000000` spreads a large design further. |
| DRCs left in congested areas | `routing-layer-adjustment` (0 to 1) withholds that fraction of each signal layer from the global router. |
| Setup misses on long nets | `placement.timing-driven: true`. On OpenROAD 26Q4 it also buffers and resizes during placement, which grows area. Needs wire RC. |
| Setup misses after CTS | `post-cts-setup-repair: true` |
| Timing misses after routing | [`global-route-repair` or `post-route-repair`](#repair-timing-after-routing) |
| Many fanout buffers on a library without a fanout limit (sky130hd) | `max-fanout: 100000` stops buffering for fanout alone; `repair_design` otherwise uses 50. |
| Cells too tight | `placement.padding` (sites, default 1) |
| Clock tree | `cts-buffer` (a list's first entry is the root buffer), `cts-sink-clustering`, `cts-apply-ndr` |
| Cells crowd macro edges, or Metal Spacing DRCs at a block edge | `placement.macro-cell-halo` (µm, default 1.0) |
| Tie cells too close to their loads | `placement.tie-separation` (µm, default 0) |

A platform that ends up with `routability-use-grt` but not `routability-driven` fails to load.

## Repair timing after routing

Post-CTS repair times the design on placement estimates. Two blocks, on the platform or the run, repair again on routed parasitics. Both cost run time and change QoR.

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

Each block runs `repair_design` when `repair-design` is set (`{}` for OpenROAD's defaults), `repair_timing -setup` when `setup-margin` is set and `repair_timing -hold` when `hold-margin` is set. At least one is required. A run's block overrides the platform's field by field, and `null` turns a repair or the whole block off. `global-route-hold-repair: true` is `global-route-repair: {hold-margin: 0}`.

**`global-route-repair`** runs after global route on its parasitic estimate, then legalizes and reroutes the changed nets (`reroute: incremental`, default) or the whole design (`full`).

**`post-route-repair`** adds the stage `post_route_repair` after detailed route. It needs the PDK's `rcx-rules`. It extracts and times the detailed route (round 0), then runs up to `rounds` (default 1) repair rounds, each in fresh OpenROAD sessions: repair on the extracted parasitics, route again, extract and time. `reroute: eco` (default) keeps the detailed route and reroutes only the changed nets; `full` routes the whole design again, costing about a global and a detailed route per round. Round k repairs with the margins plus (k − 1) × `margin-step`. `rounds` above 1 needs a margin.

The stage keeps the best round, never worse than the detailed route:

- A clean route always beats one with DRCs.
- Otherwise a round wins when no repaired side's worst slack (setup if `setup-margin` is set, hold if `hold-margin` is set; 0 when met) is lower and one is higher, without more DRCs; or when it ties on timing with fewer DRCs.

Rounds stop (`post_route_repair.stop`) when the best round is clean and meets timing (`timing_met`), a clean round does not improve (`no_improvement`), timing is not reported (`timing_not_reported`), a session fails (`session_failed`) or `rounds` run out (`last_round`). `timing_met` and `no_improvement` need a clean route, so rounds with DRCs keep going. The first clean round that does not become the best ends the stage, so no later round starts from it and raising `rounds` does not change that. A failure extracting round 0 fails the run; a failed session of a later round does not: rb warns `pnr.post_route_round_failed`, keeps the best round and qualifies the `PASS`. `finish` continues from the kept round, whose DRC report becomes `route.drc.rpt`. `pnr.post_route_round` logs each round's slack and DRCs.

## Probe a floorplan

`mode: probe` on a run checks quickly whether a floorplan places and routes. It skips setup and hold repair, stops after global route and writes the `global_route` checkpoint. The result is a `PASS` with `routed: false`, the global-route overflow and timing on the global-route estimate. `xfail:` does not apply to it, and a `closure` run cannot resume from a probe's checkpoints. A probe publishes no abstract, so `harden: true` with `mode: probe` fails to load.

## Shape the floorplan

By default the die is sized from the netlist's cell area by `floorplan.utilization`, `aspect` and `core-margin`. To fix it exactly, set `die-area` and `core-area` together (`[x0, y0, x1, y1]` in µm); they exclude the other three. `core-cutouts` carves rectangles out of the core for an L- or T-shaped core: they block cells, rows, taps and macros, not routing, and the die stays rectangular. With utilization sizing, lower `utilization` to leave room for cut-outs.

`floorplan.blockages` keep standard cells out of rectangles (die µm):

```yaml
    floorplan:
      blockages:
        - rect: [10, 10, 130, 60]                 # hard: no cells, rows cut, macros moved past it
        - rect: [400, 300, 520, 420]
          type: partial                            # global placement caps density here
          max-density: 0.4
        - rect: [200, 50, 260, 120]
          type: soft                               # global placement only; repair may use it
          lifetime: global-placement               # removed after global placement
```

`lifetime: global-placement` makes any type a screen for global placement alone: no rows are cut, macro placement ignores it and no checkpoint holds it. Blockages need OpenROAD 26Q1.

## Place macros

A netlist with hard macros (SRAMs, hardened blocks) gets them placed before the power grid. By default rb's packer puts them in rows from the core's lower-left corner, tallest first, `placement.macro-halo` (default 20 µm) apart and from the core edge, on the site grid. `floorplan.macro-anchor` starts from another corner, keeping the opposite edges free for pins. `floorplan.macro-placement: rtl-mp` uses OpenROAD's RTL-MP instead, which places by connectivity and may mirror macros; it keeps out of every blockage type and cannot take a `macro-anchor`.

`floorplan.macros` fixes, orients or haloes individual macros:

```yaml
    floorplan:
      macros:
        - instance: u_core/u_sram          # instance name or glob (* crosses /)
          location: [200, 290]             # lower-left corner after orientation, die µm
          orientation: MX
          halo: [3, 3]                     # replaces macro-cell-halo for this macro
        - instance: "*/u_rf*"
          orientation: R180
```

- A `location` is snapped to the site and row grid, must be inside the core and clear of other fixed macros, and must match one macro. Placers leave it there.
- Under `rtl-mp`, an `orientation` needs a `location`.
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

## Run Tcl hooks

`hooks:` on a run sources your Tcl files at fixed points of the flow, for steps rb does not have. Each point takes a path or a list, relative to `pnr.yaml`:

```yaml
    hooks:
      session: [wire_rc.tcl]
      after-cts: [io_latency.tcl]
```

| Point | Sourced |
| --- | --- |
| `session` | At the start of every OpenROAD session, after the SDC, before the floorplan |
| `after-floorplan` | After floorplan, tracks, blockages and tie cells |
| `macro-placement` | Instead of rb's macro placement; only when the netlist has macros. `$rb_movable_macros` lists the unlocated ones. Not with `macro-placement: rtl-mp` or a non-default `macro-anchor`. |
| `after-pdn` | After taps and the power grid |
| `after-pins` | After pin placement, before port buffering. A hook that buffers ports needs `buffer-ports: false`. |
| `after-global-placement` | After `global_placement` |
| `after-repair-design` | After `repair_design` |
| `after-cts` | After CTS, before setup and hold repair |
| `after-global-route` | After `global_route`, before global-route repair |
| `before-detailed-route` | Before each detailed route, including each post-route repair round's |
| `before-reports` | After fill and extraction, before the final reports |

Hooks run at Tcl global scope in `artefacts/<run>/`, with `$OUT_DIR` and `$DESIGN` set. A Tcl error, `exit` or stray `return` in a hook fails the run naming the point and file. Results come from the final reports, so a report a hook prints earlier does not change them. Hook files are inputs of checkpoints and hardened abstracts. On a multi-corner platform an SDC command in a hook constrains the primary corner only; see [Sign off at several corners](#sign-off-at-several-corners).

When a run splits into several OpenROAD sessions (`stages: true`, `--resume`, `--dispatch slurm`, or `post-route-repair`), Tcl procedures, `rename` wrappers, `set_wire_rc` and `set_dont_use` set by a hook are gone in the next session. Put such state in a `session` hook. rb warns `pnr.hook_commands_not_kept` when a hook redefines an existing command.

## OpenROAD threads

OpenROAD uses one thread unless the run sets `threads:` to a count or `auto`. `auto` takes the CPUs of the Slurm allocation or affinity mask the run is in, 1 outside one, never the host's core count. A count above the allocation is clamped with `openroad.threads_capped`. Run `rb pnr` inside an allocation (`srun -c 8 rb pnr ...`) to give it CPUs, or use [`--dispatch slurm`](#dispatch-the-stages-to-slurm). Results can differ slightly between thread counts. `rb power` and `rb synth`'s OpenROAD stage read `threads:` the same way.

## Keep stage checkpoints

A run killed at a wall-time limit leaves only its log. `checkpoints: true` (or one stage, or a list) writes `<NN>_<stage>.odb`, `.def` and `.sdc` at each stage boundary into `checkpoints/<run-id>/`, with `checkpoints/latest` pointing at the newest:

| Stage | Holds |
| --- | --- |
| `floorplan` | Floorplan, macros, power grid, pins |
| `place` | Legalized global placement |
| `cts` | Clock tree and hold repair |
| `global_route` | The global route after global-route repair, with route guides |
| `detailed_route` | The detailed route, before fill, with its DRC report |
| `post_route_repair` | The kept repair round (only with `post-route-repair`) |

On a multi-corner platform the `.sdc` holds every corner's SDC mode, and `<NN>_<stage>.<corner>.sdc` each corner's alone.

Look at a checkpoint in OpenROAD or with `rb pnr-export --checkpoint`. It is never a final result. Old run directories are not pruned.

## Run stages as separate sessions

`stages: true` runs each stage (`floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `post_route_repair`, `finish`) as its own OpenROAD session that reads the previous stage's checkpoint. It writes every checkpoint, keeps each session's script and log in the checkpoint directory, and concatenates them into `pnr.tcl` and `pnr.log`. A staged run gives the same result as a one-session run at the same thread count, with the exceptions in [Known Issues](../known-issues.md#a-staged-pr-run-can-differ-from-a-one-session-run).

Stop early or resume:

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --stop-after global_route
rb pnr alu_pnr -c pnr/alu/pnr.yaml --resume latest --from detailed_route
```

- `--stop-after <stage>` works on any run, writes that checkpoint and a stage report, and passes with `routed: false`. It writes no routed outputs, GDS or abstract, and `xfail:` does not apply.
- `--resume <run> --from <stage>` starts a new staged run from a staged run's checkpoint of the stage before. `<run>` is `latest`, a run id or a checkpoint directory. The old run is left unchanged, so detailed routing can be retried from one global route.
- The resume is refused (`fail_stage: resume`) when the earlier stages' generated scripts, their input files or the OpenROAD version changed, or when resuming a probe into a closure run. A `constraints:` path and a map of that path to every corner count as different. Settings read only from `--from` on, such as `detailed-route-verbose`, routing layers or `threads:`, may change. Files sourced by hooks, `platform-tcl`, a `pdn-config` or the SDC, and environment variables, are not checked.

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
- Ctrl-C, SIGTERM or SIGHUP cancels the run's jobs.
- If `rb pnr` itself is killed, its jobs keep running. Until they leave the queue, a new `rb pnr` of the same configuration refuses to start and prints the `scancel` command. See [Known Issues](../known-issues.md#an-unconfirmed-dispatched-pr-run-blocks-later-runs).
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
- An abstract whose inputs changed since hardening is refused, naming what changed. Re-harden it, or pass `--accept-stale`.
- A block hardened with `abstract: {gds: false}` has no GDS. A parent that streams out (`--gds`, `--png` or its own `harden`) or `rb pnr-export` of the parent fails at setup naming the block.

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
2. Floorplan, tracks, blockages and cut-outs, tie cells.
3. Macro placement, tap cells, power grid, pin placement, port buffering.
4. Global placement (with `lifetime: global-placement` and rotated-macro blockages present), `repair_tie_fanout`, `repair_design`, legalization.
5. CTS, optional post-CTS setup repair, hold repair, legalization.
6. Global route, optional global-route repair, detailed route, optional post-route repair stage.
7. Fill, global connections, extraction with `rcx-rules`, final reports, outputs.
8. After OpenROAD: the routed-floorplan check, then stream-out and abstract.

A `probe` skips the setup and hold repair and stops after global route. Each session must print its end marker (`>>> DONE`, `>>> End of stage <NN>_<stage>`, `>>> STOPPED after <stage>`, or the post-route round markers), or the run fails `incomplete`. This check comes before any check of the design.

A staged session starts with a prelude regenerated from the same template: the header, Liberty and LEF, `read_db` and `read_sdc` of the previous checkpoint (every mode), its saved flow variables, then step 1's per-session settings. It keeps only what the database, the checkpoint SDC and the state file hold. A resume compares each earlier stage's script up to its end-of-stage line, ignoring comments, the run-directory lines, `set_thread_count` and header variables the stage does not read.

Each stage's wall time, CPU time and peak memory are recorded as `stage_end` events in `progress.jsonl` and under `resources` in the manifest. In a one-session run the peak memory of a stage includes what earlier stages left resident.
