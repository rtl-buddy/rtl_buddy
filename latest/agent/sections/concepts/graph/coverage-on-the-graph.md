## Coverage on the Graph

`rb graph results` also joins each declared `covers:` relationship to coverage already on disk. It never reruns the simulator. See [Coverage](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/coverage/) for what the metrics mean.

The default `--coverage auto` uses the newest coverage manifest and model, and falls back to per-test `coverage.dat` files. Other choices:

- `--cov-dir` or `--cov-manifest` selects a manifest.
- A merged LCOV `.info` file can be passed as the source.
- `--coverage model` requires the model source.
- `--no-coverage` disables the join.

Each coverage item gets one state:

| State | Meaning |
| --- | --- |
| `exercised` | A declared item matched an observed cover point with hits. |
| `declared-only` | The item was declared but no matched point fired. |
| `observed-but-undeclared` | An observed cover point has no `covers:` declaration. |

Names match exactly first, then case-insensitively, then after normalisation, then with a `cov`/`cvr`/`c` affix removed. The overlay records which rule matched; an `affix` match is a prompt to align the names. LCOV has no module or per-test identity, so design coverage joins by source file, and any available per-test databases still feed test badges and coverage-item verdicts. Unresolved paths are reported, not guessed.
