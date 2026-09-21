---
description: Run multiple simulation suites from regression.yaml, filter by regression level, and choose local or parallel dispatch.
---

# Regressions

A regression runs the test suites listed in one manifest and combines their results.

## Configure a regression

```yaml
rtl-buddy-filetype: reg_config

test-configs:
  - design/block_a/verif/tests.yaml
  - design/block_b/verif/tests.yaml
```

Paths resolve from the directory containing `regression.yaml`. Each suite keeps its own artefacts and detailed log; the manifest directory receives the regression log and merged outputs.

See [YAML Formats: regression.yaml](../reference/yaml.md#regressionyaml) for the schema.

## Resolve the manifest

An explicit config wins:

```bash
rb regression --reg-config path/to/regression.yaml
```

Without it, RTL Buddy checks:

1. `./regression.yaml` in the invocation directory
2. `cfg-rtl-reg.reg-cfg-path` in `root_config.yaml`

Other flow regressions use the same order: explicit `-c`, `./<flow>_regression.yaml`, then the matching `cfg-rtl-reg.<flow>-reg-cfg-path`. Declare non-root flow manifests in `cfg-rtl-reg` so graph discovery can find them.

## Filter by regression level

Tests with `reglvl` in the selected inclusive range run; others report `SKIP`:

```bash
rb regression --reg-level 2000
rb regression --start-level 1000 --reg-level 3000
```

The default upper level is 0, so an unqualified regression runs must-run tests with `reglvl: 0`. A test may define one level or builder-specific levels. See [Tests](tests.md#filter-by-regression-level).

## Reuse compilation

When tests share compile inputs, reuse a compiled build:

```bash
rb regression --share-build
```

Verilator, VCS, and Icarus support cross-test sharing. Reuse is reported once per build directory per process on the console, and every test's `compile.log` (and the log file) records its own reuse; add `--rebuild` to compile even when the stamp says the build is current. See [Sharing compiled builds](tests.md#sharing-compiled-builds-across-tests) for invalidation and backend limitations.

## Replay a seeded regression

Pass one master seed to reproduce every selected test's runtime seed:

```bash
rb regression --master-seed 20260914
rb regression --master-seed 20260914 --dispatch slurm
```

The master seed appears once in the run summary and machine payload. Each
test's resolved seed is independent of suite order and dispatch timing, and a
dispatched plan carries both values to its worker. Repeating the command with
the same project layout and master seed reproduces the seeds without reading
old artefacts. See [Run with randomized seeds](tests.md#run-with-randomized-seeds)
for derivation, preprocessor access, fixed-test overrides, and result records.

## Run in parallel

The default `--dispatch local` runs tests sequentially in the current process. For parallel execution:

```bash
rb regression --dispatch local-parallel -j 4
rb regression --dispatch slurm
```

Dispatch implies shared builds. RTL Buddy expands each suite, creates one build job covering that suite's unique compile keys — two chained jobs where [verilation is split off](dispatch.md#split-verilation-from-the-c-build) — then runs dependent simulation jobs and combines their normal results.

`local-parallel` uses subprocesses on the current host and needs no scheduler. It cannot enforce `resources:` reservations or collect usage telemetry.

Slurm dispatch requires a Linux submit host, Slurm client commands, and a filesystem shared with compute nodes. See [Parallel Dispatch](dispatch.md) for cluster configuration, resources, failure recovery, and job accounting.

## Run two tiers at once

One regression per simulator in one checkout, concurrently, needs a per-run artefact namespace — otherwise both runs write the same `artefacts/<test>/` paths and the second dies on the [artefact-tree lock](execution-context.md#handle-an-artefact-lock). Give each run a `--run-tag`:

```bash
rb -B verilator regression --run-tag verilator &
rb -B icarus regression --run-tag icarus &
wait
rb graph results --run-tag verilator
rb graph results --run-tag icarus
```

Each run gets its own tree, its own lock, its own log, and its own results overlay under `artefacts/.runs/<tag>/`; the shared builds stay shared. See [Namespace concurrent runs](execution-context.md#namespace-concurrent-runs) for the full path table and the tag's syntax rules.

## Read the results summary

A regression prints a summary to stderr: one row per test, the metadata footer, and a tally of every verdict in the run.

```text
Results: 780 PASS, 3 FAIL, 2 SKIP (785 total)
```

Verdicts are listed in the order `PASS`, `FAIL`, `XFAIL`, `XPASS`, `SKIP`, `NA`, and the tally always counts the whole run.

On a large regression, show only the rows that need attention:

```bash
rb --print-failures-only regression -c regression.yaml
```

The flag drops `PASS`, `SKIP`, and `XFAIL` rows from the console render and keeps the tally. `rtl_buddy.log` and the machine-mode `summary` event still carry every row, so saved records and downstream parsing see the full result set.
