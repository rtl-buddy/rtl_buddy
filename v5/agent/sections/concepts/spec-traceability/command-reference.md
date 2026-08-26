## Command reference

| Command | What it checks |
|---------|----------------|
| `rb spec list` | Lists all spec blocks discovered under `spec/` (or `--spec-dir`) |
| `rb spec check-design` | For every spec block, shows whether a design model references it |
| `rb spec check-coverage` | For every coverage item, shows which tests cover it and flags uncovered items |

All three commands accept `--spec-dir` to target a subdirectory. `check-design` also accepts `--design-dir`; `check-coverage` accepts `--verif-dir`.
