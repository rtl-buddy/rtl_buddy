## Repair timing after routing

Post-CTS repair times the design on placement parasitics, which see no routed wire. Two platform blocks repair again on routed parasitics. Both are unset by default because they add run time and change QoR:

```yaml
cfg-pnr-platforms:
  - name: sky130hd_tt
    # ...
    global-route-repair:
      repair-design: {slew-margin: 20, cap-margin: 20}
      setup-margin: 0.05       # ns
      hold-margin: 0.02        # ns
      reroute: incremental     # or full
    post-route-repair:
      setup-margin: 0.05
      rounds: 3
      margin-step: 0.02
      reroute: eco             # or full
```

Each block takes:

- `repair-design`: run `repair_design` with `slew-margin` and `cap-margin` (percent, 0 to 100) and `max-wire-length` (microns, positive). `{}` runs it with OpenROAD's defaults. Unset or `null` skips it.
- `setup-margin` and `hold-margin`: run `repair_timing -setup` or `-hold` with that margin, in ns, at least 0. rb passes the margin in the Liberty time unit, so `0.05` is `-setup_margin 50` on a picosecond library and `-setup_margin 0.05` on a nanosecond one. A margin of `0` passes no option. Unset or `null` skips that repair. With `setup-margin` set, the hold repair also passes it as `-setup_margin`, as ORFS does, so the hold buffers it inserts keep that much setup slack.

A block that sets none of the three repairs is a config error, and so is `post-route-repair` with `rounds` above 1 and neither margin. Both rules apply to the block a run ends up with, the platform's merged with the run's, so a platform may set part of a block, such as `{rounds: 3, margin-step: 0.05}`, that its runs complete; a run that leaves it incomplete fails at `setup`. Each repair is logged before it runs, with its margins in ns and the unit they were passed in:

```text
>>> Global-route repair: repair_timing -setup -setup_margin 50 (setup margin 0.05 ns in Liberty time unit 1ps)
>>> Global-route repair: repair_timing -hold -hold_margin 20 -setup_margin 50 (hold margin 0.02 ns, setup margin 0.05 ns in Liberty time unit 1ps)
```

A global-route repair of only `hold-margin: 0`, which is what `global-route-hold-repair: true` sets, logs the single line `>>> Post-global-route hold repair` instead, so its flow script and checkpoints compare equal to the shorthand's.

A run sets its own `global-route-repair:` or `post-route-repair:` in `pnr.yaml` over the platform's, field by field and within `repair-design`. A run's `null` field skips that repair, and a run's `global-route-repair: null` or `post-route-repair: null` turns the platform's block off. `global-route-hold-repair: true` on a platform is `global-route-repair: {hold-margin: 0}`; a platform that sets both is a config error.

### Repair on the global route

After global route and its `after-global-route` hooks, `global-route-repair` runs `estimate_parasitics -global_routing`, the repairs and a legalization, in ORFS order. `repair_timing` reroutes the nets it changes itself. With `repair-design` and a margin set, the repair also legalizes inside an incremental global route between `repair_design` and `repair_timing`, as ORFS does: `repair_timing` builds buffer trees from the global routes, and the nets `repair_design` split have none until they are rerouted. With `reroute: incremental` (the default) the legalization, `detailed_placement` alone, runs inside `global_route -start_incremental` and `-end_incremental`, which reroutes the nets of the cells it moved; `reroute: full` runs the flow's global route again after the legalization. The closing route is `-verbose`, so `pnr.log` repeats the congestion table. The `global_route` [checkpoint](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints) holds the design after this repair.

### Repair on the detailed route

`post-route-repair` adds the stage `post_route_repair` (`06_post_route_repair`) between `detailed_route` and `finish`. It needs the PDK's [`rcx-rules`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#extract-parasitics-with-rcx-rules); without them the run fails at setup. The stage runs up to `rounds` rounds (default 1), each in fresh OpenROAD sessions:

1. Read the latest routed database (the detailed route's, then each round's), extract its parasitics into `06_post_route_repair.r<k>.spef`, read it once per corner and report setup and hold worst slack and TNS on every corner.
2. Stop when every side the block repairs meets timing: setup when `setup-margin` is set, hold when `hold-margin` is set, each with worst slack at least 0 on every corner or unconstrained (`INF`, such as hold on a block constrained only by `set_max_delay`). Also stop when the report has no slack for such a side. A side the block does not repair never starts a round, and a block with neither margin (`repair-design` only) repairs in its one round whatever the slack.
3. Otherwise read the database in a new session and repair it, then write the stage's checkpoint. The next round's extraction of that route, or the final report after the last round, is what judges the repair. With `reroute: eco` (the default) the session keeps the detailed route. It estimates placement parasitics, reads the SPEF once per corner over them, removes the fill cells and opens an incremental global route. The repairs then run with OpenROAD's `design_is_routed` answering 0, so they stay on the placement source: they time the nets they do not change on the SPEF and the nets they change on placement estimates, and build buffer trees from Steiner trees. The session legalizes and closes the incremental route, which reroutes only the nets the repair changed, runs the `before-detailed-route` hooks and routes in detail, keeping every other net's wires. With `reroute: full` it removes the fill cells and the detailed route, routes globally and reads the SPEF over the global-route estimate, so the repairs time the nets they change on the global route; as on the global route, a `repair_design` followed by a timing repair is legalized between them. It then legalizes, runs the hooks and routes every net in detail again.

Round k repairs with the configured margins plus (k - 1) × `margin-step` (default 0). A round whose detailed route reports DRC violations is the last; `finish` continues from it, and its DRC count is the run's. When no round repairs, the stage's checkpoint is the detailed route's. The stage keeps the last round that repaired even when it measured worse than an earlier one; see [Quirks & Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#post-route-repair-keeps-the-last-repaired-round). Each repairing round runs the `before-detailed-route` hooks before its detailed route. It does not run the `after-global-route` hooks: their work belongs to the `global_route` stage, and running them on each round's global route would repeat it. `finish` fills and writes the outputs from the stage's checkpoint. Quirks & Known Issues explains [why a round runs in sessions of its own and in this order](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#post-route-repair-rounds-work-around-openroad-routing-failures), [what an `eco` round depends on in OpenROAD](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#eco-post-route-repair-overrides-an-openroad-procedure) and [why a multi-corner run's rounds and final report can disagree](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#post-route-repair-rounds-and-the-final-report-can-disagree-on-a-multi-corner-platform). A session that fails fails the run in stage `06_post_route_repair`.

`pnr.post_route_round` logs each round's setup and hold WNS and TNS, the sides that are `unconstrained` and the margins it repairs with, and `progress.jsonl` records it as a `post_route_round` event; a [dispatched](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#dispatch-the-stages-to-slurm) run's head logs them from there too. `pnr.post_route_repair_done` gives how many rounds repaired and why the rounds stopped. The checkpoint run directory holds each session's script and log (`06_post_route_repair.r<k>.extract.tcl` and `.r<k>.repair.tcl`) and `06_post_route_repair.plan.json`, the rounds the stage runs.

A run with `post-route-repair` and without `stages: true` runs `pnr.tcl` through detailed route and writes the `detailed_route` checkpoint, then runs the rounds and `finish` as sessions of their own, as a [staged run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-stages-as-separate-sessions) does. Its `pnr.tcl` ends the first session with `exit` and then records the later sessions' scripts, so it holds the whole flow, and those sessions log hook commands that do not reach them as a staged run's do.
