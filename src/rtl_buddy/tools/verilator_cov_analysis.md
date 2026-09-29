# Verilator Coverage Analysis

How rtl_buddy produces and reports Verilator coverage, and how the raw, LCOV and Coverview outputs differ. For the user-facing workflow see `rb docs show concepts/coverage`.

## Coverage outputs

A coverage run produces three outputs. They do not share an accounting model, so their totals can disagree.

- Raw Verilator coverage data in `coverage.dat`.
- LCOV `.info` files for line and branch analysis.
- Coverview `.zip` packages built from processed `.info` files.

## Raw coverage points vs LCOV

**Raw Verilator coverage points** come from `coverage.dat`, written by the instrumented model during simulation. Each point is a simulator-inserted event or counter: a line block, branch, expression, toggle or user cover point. In `verilator_coverage --annotate --filter-type ...`, `Total coverage (hit/total)` counts raw points of that type, not source lines. A raw "line" point is therefore not the same as a source line with an LCOV `DA` record.

**LCOV** comes from `verilator_coverage --write-info`, which converts raw data to a source-oriented format:

- `DA:<line>,<count>` for lines and `BRDA:<line>,...` for branches.
- `LF/LH` and `BRF/BRH` for lines and branches found and hit.
- `genhtml` renders these totals as HTML.

LCOV answers how many source lines and branches were covered, not how many raw points fired.

The mapping from raw points to LCOV records is not 1:1:

- Several raw points can collapse into one source line.
- Toggle coverage has no standard LCOV equivalent.
- User coverage may not map to LCOV.
- Raw and LCOV branch records can group branches differently.

A raw-point summary can therefore disagree with the HTML line coverage.

## One-line summary

The one-line summary reports:

| Key | Source |
| --- | --- |
| `L` | LCOV `LH/LF` |
| `B` | LCOV `BRH/BRF` |
| `T` | Raw toggle coverage-point totals |
| `F` | Raw user coverage-point totals |

`L` and `B` match the LCOV HTML reports, while `T` and `F` stay available from the raw data:

```text
L:0.97 B:0.95 T:0.08 F:UNSP
```

An unsupported metric prints `UNSP`. A metric whose only source was a failed merge prints `FAIL`. `T` and `F` come only from the merged `.dat`, so a failed `verilator_coverage --write` loses them while `L` and `B` still come from the per-test LCOV exports. See `rb docs show concepts/coverage#read-a-failed-merge`.

## How coverage is generated

Single test (`rb -M cov test ...`):

1. The `cov` builder mode compiles with Verilator coverage options.
2. The simulation writes a per-test `coverage.dat`.
3. `verilator_coverage --write-info` generates LCOV.
4. rtl_buddy reads line and branch from LCOV, and toggle and user from the raw annotation output.
5. Optional post-processing builds LCOV HTML with `genhtml` and Coverview zips with `info-process`.

Regression (`rb -M cov regression --coverage-merge`):

1. Each test writes its own raw database.
2. The raw inputs are merged.
3. The merged database is converted to merged LCOV, merged HTML and a merged Coverview zip.

The project template's `cov` builder mode compiles with `--coverage-expr`, `--coverage-expr-max 256`, `--coverage-line`, `--coverage-toggle` and `--coverage-user`.

## LCOV HTML

```bash
rb -M cov test smoke_test --coverage-html -c verif/example_block/tests.yaml
cd verif/example_block
rb -M cov regression --coverage-merge --coverage-html -c regression.yaml
```

Outputs are `*.coverage.info`, `*.coverage_html/`, `coverage_merged.info` and `coverage_merge.html/`.

Line coverage comes from `DA` records and branch coverage from `BRDA` records. Use the HTML for line and branch gaps and per-file source annotation. Toggle and user coverage are not standard LCOV HTML metrics, so raw toggle-point and user-point totals are not visible there.

## Coverview packages

Coverview archives package LCOV-based results.

```bash
rb -M cov test smoke_test --coverage-coverview -c verif/example_block/tests.yaml

cd verif/example_block
rb -M cov regression --coverage-merge --coverage-coverview -c regression.yaml
rb -M cov regression --coverage-merge --coverage-coverview --coverage-per-test -c regression.yaml
```

Outputs are `coverview_<dataset>.zip` and `coverview_<dataset>_per_test.zip`, for example `coverview_example_block.zip`.

A merged dataset contains `line`, `branch`, `expression` and `toggle`, in that order. `config.json` carries `title`, `repo`, `branch`, `commit`, `timestamp` and `additional`, with values such as the suite path relative to the repo root, user name, simulator family, dataset name and whether the package is merged.

Absolute source paths are rewritten to project-relative ones (`design/...`, `verif/...`) so the viewer opens at the repo layout.

## Official Coverview and the rtl_buddy variant

Archives open in the official Coverview viewer. rtl_buddy also adds extension files for an rtl_buddy-aware variant, which the official viewer ignores.

- Official content: typed `line`, `branch`, `expression` and `toggle` datasets, the standard line `.desc` files, and the standard `config.json` metadata.
- rtl_buddy extensions: `covrby_branch_<dataset>.desc`, `covrby_expression_<dataset>.desc` and `covrby_toggle_<dataset>.desc`, plus `config.json` metadata under `additional.covrby_coverview`.

The extensions give per-test provenance for branch, expression and toggle coverage. Line provenance stays in the standard `.desc`. Extensions must stay additive: the archive must open in official Coverview, and `covrby_*` files must not replace the standard line `.desc`.

## Set up the rtl_buddy Coverview variant

Use a local Coverview checkout or fork that reads `additional.covrby_coverview`:

```bash
cd /path/to/coverview
npm install
npm run dev
```

The viewer serves at `http://localhost:5173`. Generate an archive from rtl_buddy (see [Coverview packages](#coverview-packages)) and load the zip in the app, for example `coverview_regression.zip` or `coverview_tests.yaml__smoke_test.zip`. Tooltips then show separate per-test provenance for line (standard `.desc`), branch, expression and toggle (`covrby_*.desc`).

If regression tooltips work but single-test tooltips do not:

- Rerun rtl_buddy to regenerate the zip, then reload the archive in the browser.
- Confirm the Coverview app is a version that reads `additional.covrby_coverview`.
- If the viewer looks stale, restart `npm run dev` and hard-refresh the browser.

## Install lcov on macOS

`--coverage-html` needs `genhtml`, which `lcov` provides. Without it no HTML is generated.

```bash
brew install lcov
genhtml --version
lcov --version
```

## Install info-process on macOS

`--coverage-coverview` needs `info-process`, which rtl_buddy uses to extract typed datasets, merge typed `.info` files, and pack Coverview zips. The expected revision is `cae6eaecb7487e52436f67470ae491744c7cae0c` of `https://github.com/antmicro/info-process.git`. Clone it, check out that revision, and install from the clone into the project environment:

```bash
cd /path/to/tools
git clone https://github.com/antmicro/info-process.git
cd info-process
git checkout cae6eaecb7487e52436f67470ae491744c7cae0c

cd /path/to/your/project
uv pip install -e ../tools/info-process
uv run info-process --help
```

## Manual inspection commands

```bash
# Raw annotation: all, toggle only, expression only
verilator_coverage --annotate coverage_annotated coverage.dat
verilator_coverage --annotate coverage_toggle_annotated --filter-type toggle coverage.dat
verilator_coverage --annotate coverage_expr_annotated --filter-type expression coverage.dat

# LCOV export
verilator_coverage --write-info coverage.info coverage.dat

# Merged raw coverage
verilator_coverage --write coverage_merged.dat run1/coverage.dat run2/coverage.dat

# Coverview packaging from LCOV
info-process extract --coverage-type line --output coverage_line.info coverage.info
info-process extract --coverage-type branch --output coverage_branch.info coverage.info
info-process pack \
  --output coverview_example.zip \
  --config coverview_config.json \
  --coverage-files coverage_line.info coverage_branch.info \
  --sources-root /path/to/your/project
```

`--coverage-coverview` automates the packaging steps.

## Which output to use

- LCOV HTML for source line and branch gaps.
- The one-line `L/B/T/F` summary for quick CLI status.
- Coverview for a shareable multi-dataset view, especially merged regressions and per-test analysis.
- Do not compare LCOV HTML with raw toggle coverage; their denominators differ.
