## Per-elaboration vs source-point figures

Verilator records each coverage point once per elaborated module, so one source point appears once per parameterisation. In a suite whose compile keys build the same RTL under different defines, a key that exercises none of a block leaves that block's copy uncovered whatever the rest of the suite did. Example from a seven-key suite:

| Metric | Source points | Per elaboration |
|---|---|---|
| line | 355/399 (89.0%) | 760/924 (82.3%) |
| branch | 351/374 (93.9%) | 792/954 (83.0%) |
| toggle | 140715/145692 (96.6%) | 290437/313312 (92.7%) |

rtl_buddy reports both:

- **Per elaboration** (`totals`) answers "is this point covered in every build". Use it when one compile key is what you care about.
- **Source point** (`source_totals`) answers "is this point covered by the suite", which is how a closure target is normally stated. A point counts as covered when any elaboration hit it.

Line figures are already collapsed, so the `run` and `run (source)` rows of `rb cov summary` agree on line and differ on branch, toggle, expression and cover. `rb cov summary` shows both figures, and `--by-source` ranks the coldest files by source point. `--coverage-dir-summary` is per elaboration only, because LCOV has already folded elaborations together.

Source points come from the coverage model. A run with only LCOV fallback records no module, so its two figures are equal. If the run produced no model, the summary prints `Coverage source points: unavailable (no coverage model)`.
