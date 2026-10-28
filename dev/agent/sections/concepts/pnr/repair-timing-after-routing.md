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
- With [`signoff:`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#gate-signoff) on a multi-corner platform, each side's worst slack here, and in the stop conditions below, is taken over its signoff corners only. Each `pnr.post_route_round` event adds that judged slack as `signoff_timing`, and `pnr.post_route_repair_done` names the corners in `judged_corners`.
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

- When the best round has max-slew or max-capacitance violators, the electrical round runs `repair_design` alone at these margins on that round's database and SPEF, at `repair-design.wire-rc` when that is set. It then legalizes, reroutes per `reroute`, extracts and times like any round, and becomes the kept round only if it [beats the best](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-timing-after-routing): better timing, fewer DRCs, a smaller worst violation when it trades sides, or a tie on timing and DRCs with fewer electrical violators.
- When the best round has no such violators, the round is skipped and `pnr.log` says so.
- Its number is `rounds` + 1. `post_route_repair.electrical_round` gives the round it repaired (`source_round`), its `status` (`kept`, `not_kept`, `skipped` or `failed`) and `reason`. A failed session is set aside like a round's.

With `electrical-repair` set or an electrical gate, each `pnr.post_route_round` event in `progress.jsonl` carries `max_slew_violation_count` and `max_capacitance_violation_count` over every corner and, on a multi-corner platform, each corner's in `corners`. Without either, the rounds count no violators and their scripts are unchanged.
