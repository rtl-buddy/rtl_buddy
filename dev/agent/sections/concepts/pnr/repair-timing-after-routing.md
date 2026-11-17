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

A block runs `repair_design` when `repair-design` is set (`{}` for OpenROAD's defaults), and setup or hold `repair_timing` when that margin is set. A written block runs only what it sets; the [YAML reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#synthesis-and-physical-design-tools) gives the rest of the merge rules.

### Post-route repair rounds

`post-route-repair` extracts and times the detailed route as round 0, then runs up to `rounds` (default 1) repair rounds, each in fresh sessions. Each round:

1. Repairs on the extracted parasitics, with the margins plus (k - 1) × `margin-step` in round k. `rounds` above 1 needs a margin.
2. Routes again. `reroute: eco` (default) keeps the detailed route and reroutes the changed nets; `full` reroutes everything, costing about a global and a detailed route.
3. Repairs antennas as after detailed route, with `detailed-route-antenna-repair`.
4. Extracts and times.

Each round, the electrical round included, starts with `remove_fillers`, which removes every `CORE_SPACER` instance that is not `LOCKED`. A spacer a hook placed `FIRM` or `FIXED` to reserve sites is gone after the first round; place it `LOCKED`.

Two `repair-design` keys apply to `post-route-repair` only, both off by default:

```yaml
post-route-repair:
  repair-design:
    slew-margin: 20
    wire-rc: {resistance: 0.06, capacitance: 0.17}   # kohm/um, fF/um; or {layer: M5}
    after-timing: true
  setup-margin: 0.05
```

- `wire-rc` sets the signal wire RC at which each round's repairs (`repair_design`, `repair_timing`, `after-timing-repair` hooks) estimate the nets they change. The session's RC is set for placement, but routed nets are often longer and higher in the stack: at that RC `repair_design` can leave them over their slew limit, and `repair_timing` can judge that removing a repeater from a long wire saves time.
- `{layer: M5}` takes that layer's RC per corner from `set_layer_rc`, else the LEF. Clock wire RC is unchanged, and each repair line in `pnr.log` names the signal wire RC it used.
- The session's signal wire RC, including one a `session` hook set, returns after the round's last repair. Changed nets keep the `wire-rc` estimate until OpenROAD estimates them again, such as after legalization moves their cells.
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

With [`signoff:`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#gate-signoff) on a multi-corner platform, each side's worst and total negative slack, here and in the stop rules, come from its signoff corners only; a side's TNS is its worst signoff corner's. Each `pnr.post_route_round` event adds them as `signoff_timing`, and `pnr.post_route_repair_done` lists `judged_corners`.

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
4. Is kept only if it [beats the best](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#which-round-is-kept).

Otherwise it is skipped, and `pnr.log` says so. Its number is `rounds` + 1. `post_route_repair.electrical_round` gives `source_round`, `status` (`kept`, `not_kept`, `skipped`, `failed`) and `reason`, and a failed session is set aside like any round's.

`repair_design` finds violators on the SPEF but sizes their buffers on the placement estimate at the round's wire RC, so it leaves a net whose estimate is within the limit less the margin, however far over the extraction is ([Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#post-route-repair_design-sizes-its-buffering-on-the-placement-estimate)).

- When the round is kept, rb lists each violator still on its SPEF that the estimate judges within the limit less the margin, at the scene where its extracted violation is worst. It warns `pnr.electrical_round_unseen` naming them.
- `electrical_round.unseen` gives each one's `check` (`slew` or `capacitance`), `pin`, `limit`, `extracted` and `estimated` value.
- The list leaves out ports, and pins on constant-driven, clock, `dont_touch`, special or abutment nets, which `repair_design` never repairs.
- To fix them, raise `repair-design.wire-rc` toward the extracted RC, or bound placement's wire length with `placement.repair-design.max-wire-length`.

With `electrical-repair` or an electrical gate, each `pnr.post_route_round` event carries `max_slew_violation_count` and `max_capacitance_violation_count` over every corner, and each corner's in `corners` on a multi-corner platform. Without either, rounds count no violators and their scripts are unchanged.
