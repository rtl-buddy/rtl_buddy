---
description: Run tests, regressions, synthesis, and randomized simulation in an existing RTL Buddy project.
---

# Quick Start

Run these commands from a project where RTL Buddy is installed. If `uv run rb --version` fails, see [Installation](install.md).

## Run tests

From a suite directory containing `tests.yaml`:

```bash
uv run rb test --list     # list tests
uv run rb test basic      # run one test
uv run rb test            # run every test
```

From another directory, name the suite:

```bash
uv run rb test basic --test-config path/to/tests.yaml
```

Outputs land beside `tests.yaml`, not in the directory you ran from. See [Execution Context](concepts/execution-context.md).

## Run a regression

```bash
uv run rb regression
```

The manifest is `./regression.yaml` when present, otherwise the path set in `root_config.yaml`. To choose one:

```bash
uv run rb regression --reg-config path/to/regression.yaml
```

See [Regressions](concepts/regressions.md) for level filtering and parallel dispatch.

## Run randomized tests

```bash
uv run rb test basic --rnd-new         # one run with a new seed
uv run rb randtest basic 5             # five distinct iterations
uv run rb randtest basic 5 --rnd-rpt 3 # replay iteration 3
```

Seeds are recorded with the test artefacts.

## Run synthesis

```bash
uv run rb synth --list --synth-config path/to/synth.yaml
uv run rb synth smoke_synth --synth-config path/to/synth.yaml
```

Backend and library configuration is in [Synthesis](concepts/synthesis.md).

## Inspect results

Each suite writes orchestration output to `rtl_buddy.log` and per-test output to `artefacts/<test>/`. A `randtest` iteration writes to `artefacts/<test>/run-NNNN/`, and latest-run symlinks stay at the test artefact root.

For JSON output:

```bash
uv run rb --machine test basic
```

See [Agent Use](agents.md#machine-mode) for the JSON contract and [Tests](concepts/tests.md#interpret-results) for verdicts and exit codes.

## Configure a project

- [Root Config](concepts/root-config.md) — platforms, builders, and tool paths
- [Tests](concepts/tests.md) — `tests.yaml`
- [YAML Formats](reference/yaml.md) — complete schemas
