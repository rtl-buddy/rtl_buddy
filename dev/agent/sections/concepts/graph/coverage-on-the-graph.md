## Coverage on the Graph

`rb graph results` can correlate declared `covers:` relationships with observed coverage already on disk. It never reruns the simulator or rewrites `graph.json`.

The default `--coverage auto` uses the newest coverage manifest and model, then falls back to per-test `coverage.dat` files. You can select a manifest with `--cov-dir` or `--cov-manifest`, pass a merged LCOV `.info` file, require the model source with `--coverage model`, or disable the join with `--no-coverage`.

Coverage items receive one of three states:

| State | Meaning |
| --- | --- |
| `exercised` | A declared item matched an observed cover point with hits. |
| `declared-only` | The item was declared but no matched point fired. |
| `observed-but-undeclared` | An observed cover point has no `covers:` declaration. |

Name correlation prefers exact, case-insensitive, normalized, then `cov`/`cvr`/`c`-affix matches. The selected rung is recorded; treat an `affix` match as a prompt to align the names. Module coverage joins exact elaborated names first, then names with one trailing parameterization suffix removed. Multiple elaborations are aggregated onto the source-module node.

LCOV lacks module and per-test identity. It joins design coverage by resolved file and still uses any available per-test databases for test badges and coverage-item verdicts. Unresolved, re-anchored, or unmatched paths are reported rather than guessed.

See [Coverage](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/) for metric semantics and coverage collection.
