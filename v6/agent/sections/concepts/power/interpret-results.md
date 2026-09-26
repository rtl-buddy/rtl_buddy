## Interpret results

The summary identifies the selected design source and resolved activity source, then reports total, internal, switching, and leakage power with readable SI scaling.

On a multi-corner platform, the reported watts are those of the worst corner, meaning the one with the highest design total. The result names it in `worst_corner`, and the summary shows it in a `Worst Corner` column. `corners` holds each corner's own `total_w`, `internal_w`, `switching_w` and `leakage_w`. The session chooses the worst corner itself, so `power.rpt`, `power_instances.rpt` and the published `phys-model.json` all describe that corner; the model's manifest options name it as `corner`. A multi-corner run also fails when any corner's `power.<corner>.rpt` is missing or unparseable.

A run passes when OpenROAD exits 0, emits no `[ERROR ...]` line, and produces a parseable `Total` row in `power.rpt`. It skips when filtered by `reglvl` or when its tool has no registered backend.
