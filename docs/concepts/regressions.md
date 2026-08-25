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

Verilator, VCS, and Icarus support cross-test sharing. See [Sharing compiled builds](tests.md#sharing-compiled-builds-across-tests) for invalidation and backend limitations.

## Run in parallel

The default `--dispatch local` runs tests sequentially in the current process. For parallel execution:

```bash
rb regression --dispatch local-parallel -j 4
rb regression --dispatch slurm
```

Dispatch implies shared builds. RTL Buddy expands each suite, creates one build job per unique compile key, then runs dependent simulation jobs and combines their normal results.

`local-parallel` uses subprocesses on the current host and needs no scheduler. It cannot enforce `resources:` reservations or collect usage telemetry.

Slurm dispatch requires a Linux submit host, Slurm client commands, and a filesystem shared with compute nodes. See [Parallel Dispatch](dispatch.md) for cluster configuration, resources, failure recovery, and job accounting.
