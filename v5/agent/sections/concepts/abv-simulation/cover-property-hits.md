## Cover-property hits

Cover properties land in the same `coverage.dat` Verilator emits today, so:

- `rb -M cov test ... ` continues to be the canonical path for full coverage HTML / Coverview packaging.
- With just `assertions: true` (no `-M cov`), `coverage.dat` still exists per-run because `--coverage-user` was injected — but only the user-coverage type is present. Merge with `--coverage-merge` to roll up.

See [Coverage](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/coverage/) for the merge pipeline and the
[Verilator coverage analysis note](https://github.com/rtl-buddy/rtl_buddy/blob/main/src/rtl_buddy/tools/verilator_cov_analysis.md)
for how the raw simulator coverage points relate to LCOV outputs.
