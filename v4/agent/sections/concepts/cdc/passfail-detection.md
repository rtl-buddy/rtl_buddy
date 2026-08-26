## Pass/fail detection

A run is PASS when:
1. `rtl-buddy-cdc` exits with code 0 or 1 (1 = rule violations found, the analyzer's "ran cleanly" signal), AND
2. The JSON report parses successfully, AND
3. `summary.violations` is `0`.

A run is FAIL when violations are present, when the JSON report is missing or malformed, or when the analyzer exits with any other code (typically 2 = elaboration failure). The failure description includes the violation count and points at `cdc.log` for diagnosis.

SKIP is returned when the analysis's `reglvl` is above the `-l` filter passed to `rb cdc-regression`.
