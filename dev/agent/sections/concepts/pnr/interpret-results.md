## Interpret results

`PASS` means OpenROAD ran the flow to its end and wrote its outputs, or, for `mode: probe` and `--stop-after`, to the stage it stops after, with `routed: false` and no routed outputs. Check `routed` before treating a `PASS` as a routed result. It does not mean the design meets timing or routes clean unless [signoff gates](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#gate-signoff) are set. Judge the design on these fields:

| Field | Meaning |
| --- | --- |
| `wns_setup_ps`, `wns_hold_ps` | Worst setup and hold slack. Negative misses timing. |
| `tns_ps`, `tns_hold_ps` | Total negative setup and hold slack. |
| `drc_count` | Violations left in `route.drc.rpt`. 0 is a clean route. |
| `max_slew_violation_count`, `max_capacitance_violation_count`, `max_fanout_violation_count` | Pins over a Liberty or SDC limit at any corner, listed in `electrical.rpt`. |
| `worst_path_startpoint`, `worst_path_endpoint`, `worst_path_levels`, `worst_path_resolved` | The worst setup path over every path group, at the setup-critical corner: its ends as RTL names where the netlist keeps them, its combinational cells, and whether both ends resolved. See [Find the critical path and loaded nets in the RTL](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#find-the-critical-path-and-loaded-nets-in-the-rtl). |
| `max_net_fanout` | Sink pins of the signal net of highest fanout, clock, reset-tree and supply nets excluded. |
| `macro_pin_buffered_count`, `macro_pin_unmet_count` | With `placement.macro-pin-limits`: macro pins it buffered, and those still over their limit less the margin on placement parasitics. |
| `area_um2` | Design area. |
| `routed_cell_count` | Instances of the finished design, including added tie, buffer and clock cells, excluding physical-only cells. Compare experiments on this. |
| `cell_count` | Instances of the input netlist (the summary's Cells column). |
| `physical_cell_count` | Tap, endcap, spacer and fill cells. |
| `worst_setup_corner`, `worst_hold_corner`, `corners` | Multi-corner platforms: the corner behind each WNS, and each corner's four timing values. |
| `routed`, `mode`, `stopped_after`, `global_route_overflow` | `routed: false` means the run stopped early ([probe](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#probe-a-floorplan) or `--stop-after`); its numbers are that stage's estimate, with the global-route overflow from `global_route` on. |
| `signoff` | With [`signoff:`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#gate-signoff): each gate's values and verdict. |
| `post_route_repair` | The rounds of [post-route repair](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-timing-after-routing): `kept_round`, `rounds`, `repaired_rounds`, `stop`, `reason`, `passed_over_round`, and with `electrical-repair` `electrical_round`, which, when the round is kept, lists in `unseen` the violators its `repair_design` judged within limits on the placement estimate. |
| `openroad_threads` | Requested and effective [thread counts](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#openroad-threads). `effective` is null when `threads:` is unset. |
| `gds_status`, `gds_missing_cells` | [Stream-out](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stream-out-gds-and-png) outcome. |
| `checkpoint_dir`, `checkpoint_stages`, `last_step` | With [checkpoints](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints): where they are, which stages the run reached and checkpointed (never a failed global route), and the last step reached. |
| `failed_global_route` | `<run-id>/global_route`: the checkpoint holding a failed global route's database, for `rb pnr-export --checkpoint`. |
| `allow_congestion` | `true` on a [diagnosis run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-global-routing) that let global route keep its overflow; `global_route_overflow` is then the final overflow. |

Times are in picoseconds, converted from the Liberty `time_unit`. The final timing uses extracted parasitics when the PDK sets `rcx-rules`, else the global-route estimate. A field rb could not read is left out.

A passing run's description carries qualifiers worth reading:

- `pnr.no_wire_rc`: placement and CTS ran without wire RC. See [Set wire RC](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#set-wire-rc).
- `electrical N max-slew, ... violator(s)`: set `fail-on-electrical: true` or `electrical` in `signoff.fail-on` to fail such runs.
- `post-route repair ...`: a repair round was passed over or its session failed and was set aside.
- `post-route electrical round N kept`, `not kept` or `failed`: the [electrical round](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-electrical-violators-after-routing) ran.
- `GDS incomplete: ...`: cells with no layout in `preview` mode.
- `stale block abstract(s) accepted: ...`: run with `--accept-stale`.
- `global-route.allow-congestion (diagnosis only): global route overflow N`: global route was allowed to keep its overflow. See [Tune global routing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-global-routing).

A run fails when OpenROAD exits non-zero, logs an `[ERROR ...]` line (except in a post-route repair session rb sets aside), stops before the end of its flow, or a check below fails. It is `SKIP` when `-l` filters it out or `tool:` is not `openroad`.

### Find the critical path and loaded nets in the RTL

The final reports write two reports that point into the RTL:

- `worst_paths.rpt`: the worst setup path of each path group (one per capture clock, plus `asynchronous` for recovery and removal checks and any `group_path` groups), with its slack, its logic levels and its startpoint and endpoint. On a multi-corner platform every group is timed at the setup-critical corner, the corner with the worst setup slack, which the file names. When each corner has its own SDC mode, the groups are those of that corner's mode. Logic levels count the cells on the path between the startpoint's cell and the endpoint's, neither counted, buffers and inverters included; the file gives how many of them are buffers or inverters. `timing.rpt` holds the full path.
- `fanout.rpt`: the signal nets of highest fanout, `report-fanout-nets` of them (default 10), highest first, each with its driver. Fanout is the net's sink pins: instance inputs and output ports. Fanout is a pin count, so it compares across PDKs where delays do not. Excluded are supply nets, clock nets (the STA clock network and nets CTS built), and reset-tree nets: those reaching a register's asynchronous set or clear pin, through any buffers and inverters before it.

Each point and driver is given by its RTL name where the synthesis netlist keeps one, tried in this order:

1. A top-level port, by its name.
2. A pin of an instance the netlist names itself, such as a macro or a hardened block: `u_core.u_sram.din0[3]`.
3. The net that names the pin, when the netlist declares it under a name of its own. For a startpoint or a driver that is the net its pin drives. For an endpoint it is the register's output when the cell has exactly one (Liberty clock-to-output arc) that is not a clock, so a register's endpoint reads as the register, such as `u_core.state_q[3]`; otherwise it is the net on the endpoint pin.
4. An instance with a Yosys `src` attribute, by its scope and source lines: `u_core (alu.sv:42)`.

A name made up by Yosys (`_123_`, a name with `$`) is not one of the netlist's own. A driver that P&R inserted is traced back through buffers and inverters to the first driver that resolves, and the file says how many it is behind, and `inverted` when an odd number of them are inverters. A name that does not resolve stays as OpenROAD prints it, marked `(unresolved)`, and the file gives the raw name and `src` beside every resolved one. `worst_path_resolved` is `false` unless both ends of the worst path resolve. If the reports cannot be written, `pnr.path_report_failed` warns and their fields are left out; the run's verdict does not change.

A value that cannot be computed, because a report's commands failed or found nothing, is left out of the result and is `unavailable` in its file, with the reason, never 0. A report whose commands failed part way lists nothing, since its entries would be incomplete. After the timing the final reports already run, the reports take about 0.4 s on a routed design of 373,000 instances and 60,000 nets: the fanout scan grows with the net count and the path search with the number of path groups.
