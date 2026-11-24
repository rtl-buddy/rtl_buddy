## Interpret results

The verdict says whether the flow finished; the fields say whether the design is good.

| Verdict | When |
| --- | --- |
| `PASS` | OpenROAD ran the flow to its end and wrote its outputs. A `mode: probe` or `--stop-after` run passes at the stage it stops after, with `routed: false` and no routed outputs. |
| `FAIL` | OpenROAD exits non-zero, logs an `[ERROR ...]` line (except in a post-route repair session rb sets aside or a [hook](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#run-tcl-hooks) that returns) or stops before the end of its flow, or a check fails. |
| `SKIP` | `-l` filters the run out, or `tool:` is not `openroad`. |

Check `routed` before treating a `PASS` as routed. A `PASS` does not mean the design meets timing or routes clean unless [signoff gates](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#gate-signoff) are set. Judge the design on these fields:

| Field | Meaning |
| --- | --- |
| `wns_setup_ps`, `wns_hold_ps`, `tns_ps`, `tns_hold_ps` | Worst and total negative setup and hold slack. |
| `drc_count` | Violations left in `route.drc.rpt`; 0 is a clean route. |
| `max_slew_violation_count`, `max_capacitance_violation_count`, `max_fanout_violation_count` | Pins over a Liberty or SDC limit at any corner, listed in `electrical.rpt`. |
| `worst_path_startpoint`, `worst_path_endpoint`, `worst_path_levels`, `worst_path_resolved` | The worst setup path over all path groups at the setup-critical corner: its ends as RTL names, its combinational cells, and whether both ends resolved. See [below](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#find-the-critical-path-and-loaded-nets-in-the-rtl). |
| `max_net_fanout` | Sink pins of the highest-fanout signal net, excluding clock, reset-tree and supply nets. |
| `clock_latency` | With [`clock-latency`](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#report-clock-arrival-at-chosen-sinks): each group's late clock arrival at its sinks, per corner, against its target. |
| `cts_root_taps` | With [`cts-root-taps`](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#forward-a-clock-with-a-root-tap): each pin CTS left on the root net, with that net. |
| `macro_pin_buffered_count`, `macro_pin_unmet_count` | With `placement.macro-pin-limits`: pins it buffered, and pins still over their limit less the margin on placement parasitics. |
| `area_um2` | Design area. |
| `routed_cell_count` | Instances of the finished design, with added tie, buffer and clock cells and without physical-only cells. Compare experiments on this. |
| `cell_count` | Instances of the input netlist (the summary's Cells column). |
| `physical_cell_count` | Tap, endcap, spacer and fill cells. |
| `worst_setup_corner`, `worst_hold_corner`, `corners` | Multi-corner: the corner behind each WNS, and each corner's four timing values. |
| `routed`, `mode`, `stopped_after`, `global_route_overflow` | `routed: false` is a run that stopped early; its numbers are that stage's estimate, with the overflow from `global_route` on. |
| `signoff` | Each [signoff gate's](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#gate-signoff) values and verdict. |
| `post_route_repair` | [Post-route repair](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#repair-timing-after-routing): `kept_round`, `rounds`, `repaired_rounds`, `stop`, `reason`, `passed_over_round`, and `electrical_round`, whose `unseen` lists the violators the kept electrical round's `repair_design` judged within limits on the placement estimate. |
| `openroad_threads` | Requested and effective [thread counts](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#openroad-threads). |
| `gds_status`, `gds_missing_cells` | [Stream-out](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#stream-out-gds-and-png) outcome. |
| `checkpoint_dir`, `checkpoint_stages`, `last_step` | [Checkpoint](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#keep-stage-checkpoints) location, the stages checkpointed (never a failed global route) and the last step reached. |
| `failed_global_route` | `<run-id>/global_route`, the checkpoint holding a failed global route, for `rb pnr-export --checkpoint`. |
| `allow_congestion` | `true` on a [diagnosis run](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#tune-global-routing); `global_route_overflow` is then the final overflow. |

Times are picoseconds, converted from the Liberty `time_unit`. Final timing uses extracted parasitics when the PDK sets `rcx-rules`, else the global-route estimate. A field rb could not read is left out.

A passing run's description can carry these qualifiers:

| Qualifier | Meaning |
| --- | --- |
| `pnr.no_wire_rc` | Placement and CTS ran without [wire RC](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#set-wire-rc). |
| `pnr.no_rows_skip_placement` | The top had no rows and nothing to place, so placement, CTS, the post-CTS and global-route repairs, antenna diode insertion and fill did not run. See [Assemble hardened blocks](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#assemble-hardened-blocks). |
| `electrical N max-slew, ... violator(s)` | Electrical violators remain. `fail-on-electrical: true` or `electrical` in `signoff.fail-on` fails such runs. |
| `post-route repair ...` | A repair round was passed over, or its failed session was set aside. |
| `post-route electrical round N kept`, `not kept`, `failed` | The [electrical round](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#repair-electrical-violators-after-routing) ran. |
| `GDS incomplete: ...` | Cells without layout, in `preview` mode. |
| `stale block abstract(s) accepted: ...` | `--accept-stale` was used. |
| `global-route.allow-congestion (diagnosis only): global route overflow N` | Global route kept its [overflow](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#tune-global-routing). |

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
