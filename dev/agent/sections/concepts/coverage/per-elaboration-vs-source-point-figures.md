## Per-elaboration vs source-point figures

Verilator keys every coverage point by the module it **elaborated**, so one
source point is recorded once per parameterisation. A suite whose compile keys
build the same RTL under different defines therefore scores each point several
times, and a key that exercises none of a block leaves that block's copy dark
whatever the rest of the suite did. On a seven-key suite the difference is not
marginal:

| Metric | Source points | Per elaboration |
|---|---|---|
| line | 355/399 (89.0%) | 760/924 (82.3%) |
| branch | 351/374 (93.9%) | 792/954 (83.0%) |
| toggle | 140715/145692 (96.6%) | 290437/313312 (92.7%) |

Both figures are reported, because they answer different questions:

- **Per elaboration** (`totals`) — "is this point covered in every build", the
  figure to use when one compile key is what you care about.
- **Source point** (`source_totals`) — "is this point covered by the suite",
  which is how a closure target is normally stated. A point is covered when
  **any** elaboration hit it.

The source identity is `(file, metric, line, column, point description)` — the
raw record's `f`, `t`, `l`, `n` and `o`. Only the elaborated module is dropped;
the column stays because `n` is a column in the source text and so is the same
number in every elaboration, while dropping it would fold one line's toggle bits
(or an expression's terms) into a single point. Hit counts are summed, `found`
counts distinct collapsed points, and `hit` counts those with any hits at all.
Collapsing never rounds up: a point no elaboration hit stays a miss.

The table above counts every record the suite's databases hold. rtl_buddy's own
line figure is narrower than that: the model keys a line point on the line alone
within a file, because a line is hit or it is not, so a file's and the run's line
figures have always been collapsed. `rb cov summary`'s `run` and `run (source)`
rows therefore agree on `line` and differ on branch, toggle, expression and
cover. A per-test row still counts one line record per elaboration, as the
simulator wrote it, and its own `source_totals` collapses them.

Where each figure is reported:

| Surface | Figure |
|---|---|
| `rb cov summary` | `run` and `run (source)` rows; `--by-source` reports the coldest files collapsed |
| `rb --machine cov summary`, `rb mcp` `cov_summary` | `source_totals` beside `totals`, per run, per test and per file |
| `test` / `regression` `--coverage-source-summary` | `Coverage source points <metric>:` lines and `coverage.source_summary` |
| `cov_dir/manifest.json` | `source_totals` beside `totals` |
| `cov_dir/coverage-model.json` | `source_totals` beside `totals`, per run, per test and per file |
| `--coverage-dir-summary` | per elaboration only |

The two summaries read different inputs. The directory summary is parsed from
the merged or typed LCOV `.info`, which has already folded elaborations together
by file and line and dropped every point name, so the collapsed figure cannot be
recovered from it. The source summary is computed from the coverage model, that
is from the per-test raw `.dat` databases, the only input that records the
elaborated module per point. A run with no raw database at all (an `.info`-only
fallback) records no module anywhere, so its two figures are equal — and its
per-test line row no longer differs either. Requesting the summary from a run
that produced no model at all reports `Coverage source points: unavailable (no
coverage model)` instead of zeros.
