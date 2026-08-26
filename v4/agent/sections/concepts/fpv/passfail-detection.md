## Pass/fail detection

A run is PASS when `sby` writes `PASS` to `sby_workdir/status` (or returns exit code 0 when the status file is missing).

A run is FAIL when sby writes `FAIL`, `UNKNOWN`, or `ERROR`, or when it exits non-zero. The failure description points at the counterexample trace inside `sby_workdir/engine_<N>/` so the user can open it in `gtkwave`, `surfer`, or via `rb wave-fpv` (below).

SKIP is returned when the run's `reglvl` is above the `-l` filter passed to `rb fpv-regression`.
