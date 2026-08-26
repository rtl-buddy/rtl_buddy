## Pass/fail detection

A run is PASS when:
1. `openroad` exits with code 0.
2. The log has no `[ERROR ...]` lines.
3. The `Total` line in `power.rpt` parses cleanly.

Otherwise FAIL is returned with the parser/exit-code message. SKIP is returned when the run's `reglvl` is above the `-l` filter or when `tool:` is not in the backend registry.
