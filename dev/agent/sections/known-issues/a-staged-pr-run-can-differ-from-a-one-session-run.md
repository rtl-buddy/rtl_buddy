## A staged P&R run can differ from a one-session run

A staged run runs the same flow commands as a one-session run and gives identical results at the same thread count, except where OpenROAD carries in-memory state across a stage boundary:

- The resizer keeps state from `repair_design` and `buffer_ports`. A hook that runs one of them in an earlier stage, such as `repair_design -pre_placement` in `after-pins`, can change a later repair. rb's own `buffer_ports` (floorplan) and `repair_design` (place) also run in different stages; the sky130hd template routes identically either way, which does not guarantee it for every design.
- OpenSTA keeps the supply ports `pdngen` creates bidirectional in the session that ran `pdngen` (OpenROAD#11850). The `finish` session re-asserts them as supply ports, so `<top>.routed.v` and a hardened `<top>.lib` do not depend on it, but a one-session hook that runs after `pdngen` sees them as signal ports.
- OpenROAD writes the supply ports after the buses in the session that ran `pdngen` and before them in a session that read the database. rb moves them last, sorted by name, in `<top>.routed.pwr.v` and a hardened `<top>.lib`, so neither depends on it. A file it cannot read that way keeps OpenROAD's order, and the run warns `pnr.supply_order_unverified` naming the file. rtl_buddy#924 tracks removing the rewrite once OpenROAD writes one order.
- Without `rcx-rules`, `finish` times on the global route restored from the route guides, which can miss a few pins (`EST-0026`), so slack can differ slightly. With `rcx-rules` both time the same SPEF.

Compare a staged run with a one-session run at the same `threads:` before relying on it to reproduce one.
