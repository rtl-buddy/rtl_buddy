## Interpret results

The summary identifies the selected design source and resolved activity source, then reports total, internal, switching, and leakage power with readable SI scaling.

A run passes when OpenROAD exits 0, emits no `[ERROR ...]` line, and produces a parseable `Total` row in `power.rpt`. It skips when filtered by `reglvl` or when its tool has no registered backend.
