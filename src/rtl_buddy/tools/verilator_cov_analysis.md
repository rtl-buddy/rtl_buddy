# Verilator Coverage Analysis

## Overview

This note describes how `rtl_buddy` handles Verilator coverage today.

There are three distinct coverage outputs in the current flow:

- raw Verilator coverage data in `coverage.dat`
- LCOV `.info` files for line and branch analysis
- Coverview `.zip` packages built from processed `.info` files

These outputs are related, but they do not represent the same accounting model.

## Verilator Coverage-Point Coverage vs LCOV Coverage

They measure related things, but not with the same accounting model.

### Verilator Coverage-Point Coverage

- comes from raw simulator coverage points in `coverage.dat`
- generated directly by the instrumented Verilated model during simulation
- each recorded point is a simulator-inserted event or counter, for example:
  - line block point
  - branch point
  - expression point
  - toggle point
  - user coverage point
- when `verilator_coverage --annotate --filter-type ...` prints:
  - `Total coverage (hit/total)`
  that `total` is the number of raw coverage points of that type, not the
  number of source lines

So for example:

- `line` here means Verilator line coverage points
- not necessarily the number of source lines with LCOV `DA` records

### LCOV Coverage

- comes from `verilator_coverage --write-info ...`, which converts raw Verilator
  coverage into LCOV format
- LCOV is source-oriented
- it emits records like:
  - `DA:<line>,<count>` for line coverage
  - `BRDA:<line>,...` for branch coverage
  - `LF/LH` totals for lines found and hit
  - `BRF/BRH` totals for branches found and hit
- `genhtml` then renders those LCOV source-level totals into HTML

So LCOV answers:

- how many source lines were covered
- how many source branches were covered

not:

- how many raw simulator coverage points fired

### Why They Differ

The mapping from raw coverage points to source-level LCOV records is not 1:1.

Examples:

- multiple raw points may collapse into one source line in LCOV
- Verilator toggle coverage has no standard LCOV equivalent
- user coverage may not map to LCOV at all
- branch accounting in raw Verilator and LCOV branch records can use different
  grouping

This is why a raw-point summary can disagree with the HTML line coverage.

## rtl-buddy Coverage Policy

For `rtl_buddy` one-line summaries:

- `L` comes from LCOV `LH/LF`
- `B` comes from LCOV `BRH/BRF`
- `T` comes from raw Verilator toggle coverage-point totals
- `F` comes from raw Verilator user coverage-point totals
- unsupported metrics print `UNSP`

This makes the one-line `L` and `B` values consistent with the LCOV HTML
reports while keeping `T` and `F` available from the raw Verilator coverage
data model.

Current example:

```text
L:0.97 B:0.95 T:0.08 F:UNSP
```

## How rtl-buddy Generates Coverage

### Single-Test Flow

1. `rtl_buddy -M cov test ...` compiles with Verilator coverage options enabled.
2. The simulation writes raw coverage data to a per-test `coverage.dat`.
3. `rtl_buddy` runs `verilator_coverage --write-info ...` to generate LCOV.
4. `rtl_buddy` parses:
   - line and branch from LCOV
   - toggle and user from raw Verilator annotation output
5. Optional post-processing generates:
   - LCOV HTML with `genhtml`
   - Coverview zip packages with `info-process`

### Regression Flow

1. Each test produces its own raw coverage database.
2. `rtl_buddy --coverage-merge` merges the raw coverage inputs.
3. The merged raw database is converted to:
   - merged LCOV
   - merged HTML
   - merged Coverview zip

### Current Verilator Coverage Options

In this repo, the Verilator `cov` mode enables:

- `--coverage-expr`
- `--coverage-expr-max 256`
- `--coverage-line`
- `--coverage-toggle`
- `--coverage-user`

## LCOV HTML Workflow

### Generate HTML from rtl_buddy

Single test:

```bash
./venv/bin/python -m rtl_buddy -M cov test smoke_test --coverage-html -c verif/example_block/tests.yaml
```

Regression with merged HTML:

```bash
cd verif/example_block
../../venv/bin/python -m rtl_buddy -M cov regression --coverage-merge --coverage-html -c regression.yaml
```

Typical outputs:

- `*.coverage.info`
- `*.coverage_html/`
- `coverage_merged.info`
- `coverage_merge.html/`

### What the HTML Represents

- line coverage comes from LCOV `DA` records
- branch coverage comes from LCOV `BRDA` records
- toggle and user coverage are not standard LCOV HTML metrics

So the HTML is the right place to inspect:

- line coverage gaps
- branch coverage gaps
- per-file source annotation

It is not the right place to inspect:

- raw toggle-point coverage
- raw user coverage-point totals

## Coverview Workflow

`rtl_buddy` can package LCOV-based results into Coverview archives.

### Generate Coverview from rtl_buddy

Single test:

```bash
./venv/bin/python -m rtl_buddy -M cov test smoke_test --coverage-coverview -c verif/example_block/tests.yaml
```

Merged regression package:

```bash
cd verif/example_block
../../venv/bin/python -m rtl_buddy -M cov regression --coverage-merge --coverage-coverview -c regression.yaml
```

Per-test regression datasets for Coverview:

```bash
cd verif/example_block
../../venv/bin/python -m rtl_buddy -M cov regression \
  --coverage-merge \
  --coverage-coverview \
  --coverage-per-test \
  -c regression.yaml
```

Typical outputs:

- `coverview_<dataset>.zip`
- `coverview_<dataset>_per_test.zip`

Examples:

- `coverview_example_block.zip`
- `coverview_example_block_per_test.zip`

### What rtl_buddy Puts into Coverview

For merged datasets, `rtl_buddy` currently packages:

- `line`
- `branch`
- `expression`
- `toggle`

in that order.

The package also includes metadata in Coverview `config.json`, such as:

- `title`
- `repo`
- `branch`
- `commit`
- `timestamp`
- `additional`

with current values like:

- suite path relative to repo root
- user name
- simulator family
- dataset name
- whether the package is merged

### Source Paths in Coverview

`rtl_buddy` rewrites absolute source paths to project-relative paths before
packing the Coverview archive. This keeps the viewer rooted at the repo layout,
for example:

- `design/...`
- `verif/...`

instead of making users descend through absolute filesystem prefixes.

### Official Coverview vs rtl-buddy Coverview Variant

`rtl_buddy` packages Coverview archives so they remain usable in the official
Coverview viewer, but it also adds local extension files for an `rtl_buddy`
aware Coverview variant.

Official-compatible content:

- standard typed coverage datasets:
  - `line`
  - `branch`
  - `expression`
  - `toggle`
- standard line `.desc` files paired with the `line` dataset
- standard Coverview metadata in `config.json`

Local `rtl_buddy` extension content:

- `covrby_branch_<dataset>.desc`
- `covrby_expression_<dataset>.desc`
- `covrby_toggle_<dataset>.desc`
- `config.json` metadata under:
  - `additional.covrby_coverview`

These `covrby_*` files provide per-test provenance for:

- branch coverage
- expression coverage
- toggle coverage

The official Coverview viewer ignores those extra files safely.

## Setting Up the rtl-buddy Coverview Variant

If you want the extra per-type provenance tooltips from `rtl_buddy`, use the
local Coverview repo variant instead of only the official upstream viewer.

Use a local Coverview checkout or fork that understands the optional
`additional.covrby_coverview` metadata emitted by `rtl_buddy`.

### Install the Viewer

```bash
cd /path/to/coverview
npm install
```

### Run the Viewer

```bash
cd /path/to/coverview
npm run dev
```

Typical local URL:

```text
http://localhost:5173
```

### Load rtl-buddy Coverview Archives

Generate a package from `rtl_buddy`:

```bash
cd /path/to/your/project/verif/example_block
../../venv/bin/python -m rtl_buddy -M cov regression --coverage-merge --coverage-coverview -c regression.yaml
```

or for a single test:

```bash
cd /path/to/your/project/verif/example_block
../../venv/bin/python -m rtl_buddy -M cov test smoke_test --coverage-coverview -c tests.yaml
```

Then load the resulting zip in the local Coverview app, for example:

- `coverview_regression.zip`
- `coverview_tests.yaml__smoke_test.zip`

### What the rtl-buddy Variant Adds

With the local variant, tooltips can show separate per-test provenance for:

- `line`
- `branch`
- `expression`
- `toggle`

The provenance source is:

- `line`: standard line `.desc`
- `branch`: `covrby_branch_*.desc`
- `expression`: `covrby_expression_*.desc`
- `toggle`: `covrby_toggle_*.desc`

### Compatibility Rule

Keep `rtl_buddy` packaging official-Coverview-compatible by default.

That means:

- the archive must still open in official Coverview
- local-only enhancements should stay additive
- `covrby_*` files should not replace the standard line `.desc` contract

### Troubleshooting the rtl-buddy Variant

If merged regression tooltips work but single-test tooltips do not:

- regenerate the zip after rerunning `rtl_buddy`
- reload the archive in the browser
- make sure the local Coverview app is using the version that reads:
  - `additional.covrby_coverview`

If the viewer appears stale:

```bash
cd /path/to/coverview
npm run dev
```

then hard-refresh the browser.

## Installing LCOV on macOS

`rtl_buddy --coverage-html` depends on `genhtml`, which is provided by `lcov`.

Recommended install on macOS:

```bash
brew install lcov
```

Verify:

```bash
genhtml --version
lcov --version
```

If `genhtml` is missing, `rtl_buddy --coverage-html` cannot generate HTML
reports.

## Installing Coverview Workflow Tools on macOS

`rtl_buddy --coverage-coverview` depends on `info-process`.

Current expected `info-process` repo and revision:

- repo: `https://github.com/antmicro/info-process.git`
- commit: `cae6eaecb7487e52436f67470ae491744c7cae0c`

Recommended setup is to clone the repo locally, check out the pinned revision,
and install from that local checkout into the project venv.

Clone and pin:

```bash
cd /path/to/tools
git clone https://github.com/antmicro/info-process.git
cd info-process
git checkout cae6eaecb7487e52436f67470ae491744c7cae0c
```

Install into your project venv from the local clone:

```bash
cd /path/to/your/project
./venv/bin/pip install -e ../tools/info-process
```

Verify:

```bash
./venv/bin/info-process --help
```

`rtl_buddy` uses `info-process` to:

- extract typed coverage datasets
- merge typed `.info` files in info-process merge mode
- package Coverview zip archives

## Manual Commands for Inspection

### Raw Verilator Annotation

Line:

```bash
verilator_coverage --annotate coverage_annotated coverage.dat
```

Toggle only:

```bash
verilator_coverage --annotate coverage_toggle_annotated --filter-type toggle coverage.dat
```

Expression only:

```bash
verilator_coverage --annotate coverage_expr_annotated --filter-type expression coverage.dat
```

### LCOV Export

```bash
verilator_coverage --write-info coverage.info coverage.dat
```

### Merged Raw Coverage

```bash
verilator_coverage --write coverage_merged.dat run1/coverage.dat run2/coverage.dat
```

### Coverview Packaging with info-process

Once LCOV exists, a manual flow is:

```bash
./venv/bin/info-process extract --coverage-type line --output coverage_line.info coverage.info
./venv/bin/info-process extract --coverage-type branch --output coverage_branch.info coverage.info
./venv/bin/info-process pack \
  --output coverview_example.zip \
  --config coverview_config.json \
  --coverage-files coverage_line.info coverage_branch.info \
  --sources-root /path/to/your/project
```

`rtl_buddy --coverage-coverview` automates that packaging flow.

## Practical Guidance

- use LCOV HTML when debugging source line and branch coverage
- use the one-line `L/B/T/F` summary for quick CLI coverage status
- use Coverview when you want a packaged, shareable multi-dataset coverage
  view, especially for merged regressions or per-test regression analysis
- do not expect LCOV HTML and raw Verilator toggle coverage to represent the
  same denominator
