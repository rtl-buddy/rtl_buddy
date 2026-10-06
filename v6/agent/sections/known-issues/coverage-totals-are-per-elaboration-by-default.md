## Coverage totals are per elaboration by default

Verilator scores each coverage point once per module elaboration. If a suite builds the same RTL under different defines or parameters, a block can look short on coverage when it is fully covered once the copies are collapsed.

- `rb cov summary`, `--coverage-dir-summary` and merged totals report the per-elaboration figure.
- For the collapsed figure, read `source_totals`: the `run (source)` row of `rb cov summary`, `rb cov summary --by-source`, `--coverage-source-summary` on `test` and `regression`, or the `/cov` pane's `figures` picker.

See [Coverage](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/coverage/#per-elaboration-vs-source-point-figures).
