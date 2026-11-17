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

- `wire-rc`: each round's repairs, `repair_design`, `repair_timing` and the `after-timing-repair` hooks, estimate the nets they change at this signal wire RC instead of the session's, which is set for placement. Routed nets are often longer and higher in the stack than that estimate assumes, so at the session's RC `repair_design` can leave them over their slew limit and `repair_timing` can judge that removing a repeater from a long wire saves time. `{layer: M5}` takes that layer's RC per corner from `set_layer_rc`, else from the LEF. The session's signal wire RC, including one a `session` hook set, is restored after the round's last repair. The changed nets keep their `wire-rc` estimate until OpenROAD estimates them again, for example after legalizing moves their cells. Clock wire RC is not changed. Each repair line in `pnr.log` names the signal wire RC it ran at.
- `after-timing: true`: each round runs `repair_design` again after setup and hold repair, before legalizing. `repair_timing` checks the slew of the nets it changes at the session's RC, and some end a few picoseconds over their limit. It needs `setup-margin` or `hold-margin`.

The stage keeps the best round, never worse than the detailed route:

- A clean route always beats one with DRCs.
- Otherwise a round wins when no repaired side's worst slack (setup if `setup-margin` is set, hold if `hold-margin` is set; 0 when met) is lower and one is higher, without more DRCs; or when it ties on timing with fewer DRCs.
- When a round is better than the best on one repaired side and worse on the other, without more DRCs, it wins when its total negative slack, summed over the repaired sides, is smaller. So a round that closes hold and cuts setup TNS becomes the best even when one setup path is a few picoseconds worse, and the next round starts from it. When the total negative slack ties or a round's is not reported, the smaller worst violation, the lowest worst slack over the repaired sides, decides; on equal worst violations the best stays.
- With [`signoff:`](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#gate-signoff) on a multi-corner platform, each side's worst slack and total negative slack here, and in the stop conditions below, are taken over its signoff corners only, a side's total negative slack being that of its worst signoff corner. Each `pnr.post_route_round` event adds that judged worst and total negative slack as `signoff_timing`, and `pnr.post_route_repair_done` names the corners in `judged_corners`.
- When the rounds count electrical violators (with `electrical-repair` set or an electrical gate), a round that ties the best on every repaired side's worst slack and on DRCs wins when it has fewer max-slew plus max-capacitance violators. The stop reason names that tie-break. On a run that gates on electrical violators (`fail-on-electrical: true`, or `electrical` in `signoff.fail-on`), the stop reason also names the kept round's violators.

Rounds stop (`post_route_repair.stop`) when the best round is clean and meets timing (`timing_met`), a clean round does not improve (`no_improvement`), timing is not reported (`timing_not_reported`), a session fails (`session_failed`) or `rounds` run out (`last_round`). `timing_met` and `no_improvement` need a clean route, so rounds with DRCs keep going. The first clean round that does not become the best ends the stage, so no later round starts from it and raising `rounds` does not change that. When the rounds count electrical violators (with `electrical-repair` set or an electrical gate), they also keep going while the best round meets timing but has max-slew or max-capacitance violators and the block sets `repair-design`, and after a clean round that does not become the best but has fewer such violators than the best. A failure extracting round 0 fails the run; a failed session of a later round does not: rb warns `pnr.post_route_round_failed`, keeps the best round and qualifies the `PASS`. `finish` continues from the kept round, whose DRC report becomes `route.drc.rpt`. `pnr.post_route_round` logs each round's slack and DRCs, and names the comparison with the best round that decided whether it became the best in `decided_by`: `drc_violations`, `timing`, `total_negative_slack`, `worst_violation`, `electrical_violators`, or `tie` when the best stays. When a trade of sides made the kept round the best, or a clean round that trades sides with the best stopped the rounds, the stop reason gives both rounds' total negative slack and worst violations.

### Repair electrical violators after routing

A round's reroute can leave nets over their max-slew or max-capacitance limit. `electrical-repair` adds one round after the others:

```yaml
post-route-repair:
  repair-design: {slew-margin: 20, cap-margin: 20}
  setup-margin: 0.05
  electrical-repair: {slew-margin: 10, cap-margin: 10}   # percent; {} for OpenROAD's defaults
```

- When the best round has max-slew or max-capacitance violators, the electrical round runs `repair_design` at these margins on that round's database and SPEF, at `repair-design.wire-rc` when that is set. When the block sets `setup-margin` or `hold-margin`, setup and hold repair follow at the block's margins (without `margin-step`), with the `after-timing-repair` hooks, then `repair_design` again, so the buffers that clear the violators do not hand back a hold failure. It then legalizes, reroutes per `reroute`, extracts and times like any round, and becomes the kept round only if it [beats the best](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#repair-timing-after-routing): better timing, fewer DRCs, a smaller total negative slack when it trades sides, or a tie on timing and DRCs with fewer electrical violators.
- When the best round has no such violators, the round is skipped and `pnr.log` says so.
- Its number is `rounds` + 1. `post_route_repair.electrical_round` gives the round it repaired (`source_round`), its `status` (`kept`, `not_kept`, `skipped` or `failed`) and `reason`. A failed session is set aside like a round's.
- `repair_design` finds violators on the SPEF but sizes the buffers that repair them on the placement estimate at the round's signal wire RC, so it leaves a net whose estimate is within its limit less the margin, however far over the extraction is (see [Known Issues](https://rtl-buddy.github.io/rtl_buddy/v8/known-issues/#post-route-repair_design-sizes-its-buffering-on-the-placement-estimate)). When the round is kept, its extraction lists each violator still on its SPEF that the estimate judges within the limit less the margin, at the scene where its extracted violation is worst. It leaves out ports and pins on constant-driven, clock, dont_touch, special or abutment nets, which `repair_design` does not repair on any parasitics. rb warns `pnr.electrical_round_unseen` naming them, and `electrical_round.unseen` lists each one's `check` (`slew` or `capacitance`), `pin`, `limit`, `extracted` and `estimated` value. Raise `repair-design.wire-rc` toward the extracted RC, or bound placement's wire length with `placement.repair-design.max-wire-length`.

With `electrical-repair` set or an electrical gate, each `pnr.post_route_round` event in `progress.jsonl` carries `max_slew_violation_count` and `max_capacitance_violation_count` over every corner and, on a multi-corner platform, each corner's in `corners`. Without either, the rounds count no violators and their scripts are unchanged.
