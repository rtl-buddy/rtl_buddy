## Interpret results

The summary names the design source and activity source, then reports total, internal, switching, and leakage power in SI units.

On a multi-corner platform the reported watts are those of the worst corner, the one with the highest design total. `worst_corner` names it, `corners` holds every corner's four figures, and `power.rpt`, `power_instances.rpt` and the physical model describe that corner.

A run passes when OpenROAD exits 0, emits no `[ERROR ...]` line, and produces a parseable `Total` row in `power.rpt`; on a multi-corner platform every corner's report must parse. It skips when filtered by `reglvl` or when its tool has no backend.
