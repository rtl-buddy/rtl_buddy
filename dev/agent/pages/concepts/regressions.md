---
description: Run multiple simulation suites from regression.yaml, filter by regression level, and choose local or parallel dispatch.
---

# Regressions

A regression runs the test suites listed in one manifest and combines their results. Use it to run many suites in one command, at a chosen regression level, locally or in parallel.

## Configure a regression

```yaml
rtl-buddy-filetype: reg_config

test-configs:
  - design/block_a/verif/tests.yaml
  - design/block_b/verif/tests.yaml
```

Paths resolve from the directory containing `regression.yaml`. Each suite keeps its own artefacts and detailed log; the manifest directory receives the regression log and merged outputs. See [YAML Formats: regression.yaml](../reference/yaml.md#regressionyaml) for the schema.

## Resolve the manifest

An explicit config wins:

```bash
rb regression --reg-config path/to/regression.yaml
```

Otherwise RTL Buddy checks, in order:

1. `./regression.yaml` in the invocation directory.
2. `cfg-rtl-reg.reg-cfg-path` in `root_config.yaml`.

Other flow regressions follow the same order: explicit `-c`, then `./<flow>_regression.yaml`, then `cfg-rtl-reg.<flow>-reg-cfg-path`. Declare non-root flow manifests in `cfg-rtl-reg` so graph discovery can find them.

## Filter by regression level

Tests whose `reglvl` falls in the inclusive range run; the rest report `SKIP`:

```bash
rb regression --reg-level 2000
rb regression --start-level 1000 --reg-level 3000
```

The default upper level is 0, so an unqualified regression runs only tests with `reglvl: 0`. A test may set one level or builder-specific levels; see [Tests](tests.md#filter-by-regression-level).

## Reuse compilation

```bash
rb regression --share-build
```

When tests share compile inputs this reuses one build. Reuse is reported once per build directory on the console, and each test's `compile.log` and the log file record their own. `--rebuild` compiles even when the stamp says the build is current. Verilator, VCS and Icarus support sharing; see [Sharing compiled builds](tests.md#sharing-compiled-builds-across-tests) for invalidation and limits.

## Replay a seeded regression

One master seed reproduces every selected test's runtime seed:

```bash
rb regression --master-seed 20260914
rb regression --master-seed 20260914 --dispatch slurm
```

The master seed appears once in the run summary and machine payload. Each test's seed is independent of suite order and dispatch timing, and a dispatched plan carries the seeds to its workers. The same command on the same project layout reproduces the seeds without old artefacts. See [Run with randomized seeds](tests.md#run-with-randomized-seeds).

## Run in parallel

The default `--dispatch local` runs tests sequentially in the current process. For parallel execution:

```bash
rb regression --dispatch local-parallel -j 4
rb regression --dispatch slurm
```

- Dispatch implies shared builds. RTL Buddy expands each suite, creates one build job for the suite's unique compile keys (two chained jobs when [verilation is split off](dispatch.md#split-verilation-from-the-c-build)), runs the dependent simulation jobs and combines their results.
- `local-parallel` uses subprocesses on the current host and needs no scheduler. It cannot enforce `resources:` reservations or collect usage telemetry.
- Slurm needs a Linux submit host, Slurm client commands and a filesystem shared with the compute nodes.

See [Parallel Dispatch](dispatch.md) for cluster configuration, resources, failure recovery and job accounting.

## Run two tiers at once

Two regressions in one checkout write the same `artefacts/<test>/` paths, and the second fails on the [artefact-tree lock](execution-context.md#handle-an-artefact-lock). Give each a `--run-tag`:

```bash
rb -B verilator regression --run-tag verilator &
rb -B icarus regression --run-tag icarus &
wait
```

Each run gets its own tree, lock, log and results overlay under `artefacts/.runs/<tag>/`, while shared builds stay shared. Convert each run's results with `rb graph results --run-tag <tag>`. See [Namespace concurrent runs](execution-context.md#namespace-concurrent-runs) for the path table and tag rules.

## Read the results summary

A regression prints a summary to stderr: one row per test, a metadata footer, and a tally of every verdict in the run, in the order `PASS`, `FAIL`, `XFAIL`, `XPASS`, `SKIP`, `NA`:

```text
Results: 780 PASS, 3 FAIL, 2 SKIP (785 total)
```

On a large regression, show only rows that need attention:

```bash
rb --print-failures-only regression -c regression.yaml
```

The flag drops `PASS`, `SKIP` and `XFAIL` rows from the console and keeps the tally. `rtl_buddy.log` and the machine-mode `summary` event still carry every row.
