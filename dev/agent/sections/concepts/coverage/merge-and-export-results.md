## Merge and export results

Choose at most one merge mode:

| Flag | Processing | Supported outputs |
|---|---|---|
| `--coverage-merge` | Raw merge for summary/HTML; info-process for Coverview | Summary, HTML, Coverview |
| `--coverage-merge-raw` | Raw Verilator merge | Summary, HTML, Coverview |
| `--coverage-merge-info-process` | info-process only | Summary, Coverview; no HTML |

Without a merge flag, coverage remains per test.

```bash
rb -M cov regression --coverage-merge --coverage-html
rb -M cov regression --coverage-merge --coverage-coverview
rb -M cov regression --coverage-coverview --coverage-per-test
```

HTML requires `use-lcov: true` and `genhtml`; diagnose it with `rb tool-check --explain lcov`. Output is written to `coverage_merge.html` under the command root.

Coverview is an optional archive export for CI or handoff. Use `rb cov` or the hub coverage pane for interactive inspection; Coverview rendering depends on external `info-process` and compatible Coverview tooling.

### Read a failed merge

The one-line summary uses two different tokens for a missing number:

| Token | Meaning |
|---|---|
| `UNSP` | The metric was never measured: not instrumented, or not representable in the artefact the number was read from. An LCOV `.info` carries no toggle, expression, or functional coverage. |
| `FAIL` | The metric was measured and the measurement was lost: the tool run that was its only source failed. |

`verilator_coverage --write` is the only source for toggle, expression, and functional coverage under `--coverage-merge` and `--coverage-merge-raw`. When it fails — killed by the environment, out of memory, or exiting non-zero — the per-test LCOV exports still succeed, so line and branch report normally and the rest reads `FAIL`:

```text
Merged Coverage: L:0.92 B:0.95 T:FAIL F:FAIL
Coverage merge FAILED: verilator_coverage --write wrote no merged database, so toggle, expression, functional read FAIL (measurement lost), not UNSP (not instrumented) — see the coverage.merge.failed event
```

The run then **exits 1**. Results, side-cars, the coverage model, and the manifest are all written first, so nothing the run produced is lost; only the status reports that the requested measurement is incomplete. `coverage.merge.failed` carries the tool's return code and output, and `coverage.merge.degraded` records the escalation.

Machine and artefact consumers read the same fact explicitly:

- `payload.coverage.merge_failed` (bool) and `payload.coverage.failed_metrics` (metric names) on `test` and `regression`;
- `merge_failed` and `failed_metrics` at the top level of `cov_dir/manifest.json`, and in the `payload.coverage.artefacts` block beside it;
- `merge_failed` and `failed_metrics` on `rb cov summary` and `rb cov module` payloads.

Both keys are always present, so an absent key never means "the merge was fine". Under `merge_mode: "raw"`, `merged.raw` is also `null` when the merge produced nothing — a signal a consumer can still use, but no longer has to infer.

Manifest `totals` is deliberately unchanged by a failed merge. It is computed from the per-test databases rather than the merged one, so it remains a real measurement of what those databases hold; blanking a metric there would discard data the run did produce. A failed merge costs the *merged summary* number, which is why the verdict sits beside `totals` instead of inside it. Compare `merge_failed` before comparing manifest totals against a console summary.

Add directory rollups with repeatable repo-relative prefixes or a file containing one prefix per line:

```bash
rb -M cov regression --coverage-merge \
  --coverage-dir-summary src/core \
  --coverage-dir-summary src/mem

rb -M cov regression --coverage-merge \
  --coverage-dir-summary-file coverage_dirs.txt
```

Add the run's source-point figures with `--coverage-source-summary`:

```bash
rb -M cov regression --coverage-merge --coverage-source-summary
```

See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/) for the complete option set.
