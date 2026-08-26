## Pass/fail detection

A run is PASS when:
1. `openroad` exits with code 0.
2. The log has no `[ERROR ...]` lines.

Otherwise FAIL is returned with the exit code or error count in the description. SKIP is returned when the run's `reglvl` is above the `-l` filter or when `tool:` is not `openroad`.
