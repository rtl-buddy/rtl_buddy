## Troubleshooting

- Exit 2 with a configuration error after adding a coverage flag: no non-skipped test would produce raw coverage. Check that `-M cov` is set and at least one test runs. A selection of only skipped tests is not an error.
- `Coverage merge FAILED` or `FAIL` in the summary: see [Read a failed merge](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/#read-a-failed-merge).
- No HTML: set `use-lcov: true`, install `genhtml`, and do not use `--coverage-merge-info-process`.
- `Coverage source points: unavailable (no coverage model)`, or `rb cov` reporting `--coverage-model none`: the run wrote no model. Rerun with `--coverage-model full` or `totals`.
- `rb cov module` exits 2: the module is not recorded. Use one of the listed candidates.
