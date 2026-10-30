## Interpret results

`PASS` means OpenROAD ran the flow to its end and wrote its outputs, or, for `mode: probe` and `--stop-after`, to the stage it stops after, with `routed: false` and no routed outputs. Check `routed` before treating a `PASS` as a routed result. It does not mean the design meets timing or routes clean unless [signoff gates](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#gate-signoff) are set. Judge the design on these fields:

| Field | Meaning |
| --- | --- |
| `wns_setup_ps`, `wns_hold_ps` | Worst setup and hold slack. Negative misses timing. |
| `tns_ps`, `tns_hold_ps` | Total negative setup and hold slack. |
| `drc_count` | Violations left in `route.drc.rpt`. 0 is a clean route. |
| `max_slew_violation_count`, `max_capacitance_violation_count`, `max_fanout_violation_count` | Pins over a Liberty or SDC limit at any corner, listed in `electrical.rpt`. |
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
