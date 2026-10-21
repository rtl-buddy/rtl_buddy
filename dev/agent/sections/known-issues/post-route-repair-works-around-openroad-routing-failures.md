## Post-route repair works around OpenROAD routing failures

Each post-route repair round runs in fresh sessions and orders its steps to avoid these OpenROAD 26Q4 failures:

- A repair in the session that ran detailed route makes the next detailed route fail with `DRT-0218`, so each round reads the routed database in a new session.
- On a routed database, `repair_timing` switches to detailed-routing parasitics and builds buffer trees from global routes that a fresh session rebuilds incompletely, failing with `RSZ-0074` (OpenROAD#11852). An `eco` round therefore replaces OpenROAD's `design_is_routed` with one that answers 0 during its repairs, keeping them on placement parasitics. When `repair_timing` does not call that procedure, rb warns `pnr.post_route_eco_unguarded`; if the round then fails `RSZ-0074`, it is set aside and the result names the fix, `post-route-repair.reroute: full`.
- `global_route -start_incremental` around a resizer command that reroutes by itself delivers each netlist change twice, and a later rip-up fails with `GRT-0229` and `usage=65535` (OpenROAD#11851). A `full` round and the global-route repair run their repairs first and open the incremental route around the legalization only, as ORFS does. An `eco` round, whose repairs do not reroute, opens it before the repairs.
- `repair_timing` on global-route parasitics fails `RSZ-0074` on nets `repair_design` split until they are rerouted, so a repair that runs both legalizes and reroutes between them.
