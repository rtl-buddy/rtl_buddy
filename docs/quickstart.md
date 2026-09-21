---
description: Run tests, regressions, synthesis, and randomized simulation in an existing RTL Buddy project.
---

# Quick Start

Run these commands from an installed project. See [Installation](install.md) if `uv run rb --version` fails.

## Run tests

From a suite directory containing `tests.yaml`:

```bash
uv run rb test --list
uv run rb test basic
uv run rb test
```

The first command lists tests, the second runs `basic`, and the third runs every test. From another directory, identify the suite explicitly:

```bash
uv run rb test basic --test-config path/to/tests.yaml
```

Outputs land beside `tests.yaml`, not in the directory where you invoked the command. See [Execution Context](concepts/execution-context.md).

## Run a regression

```bash
uv run rb regression
```

This uses `./regression.yaml` when present, then the path configured in `root_config.yaml`. To choose another manifest:

```bash
uv run rb regression --reg-config path/to/regression.yaml
```

See [Regressions](concepts/regressions.md) for level filtering and parallel dispatch.

## Run randomized tests

```bash
uv run rb test basic --rnd-new
uv run rb randtest basic 5
uv run rb randtest basic 5 --rnd-rpt 3
```

These commands run once with a new seed, run five distinct iterations, and replay iteration 3 respectively. Seeds are recorded with the test artefacts.

## Run synthesis

```bash
uv run rb synth --list --synth-config path/to/synth.yaml
uv run rb synth smoke_synth --synth-config path/to/synth.yaml
```

The required backend and library configuration is covered in [Synthesis](concepts/synthesis.md).

## Inspect results

Each suite writes orchestration output to `rtl_buddy.log` and per-test output under `artefacts/<test>/`. A `randtest` iteration uses `artefacts/<test>/run-NNNN/`; latest-run symlinks remain at the test artefact root.

For programmatic output:

```bash
uv run rb --machine test basic
```

See [Agent Use](agents.md#machine-mode) for the JSON contract and [Tests](concepts/tests.md#interpret-results) for verdicts and exit codes.

## Configure a project

- [Root Config](concepts/root-config.md) — platforms, builders, and tool paths
- [Tests](concepts/tests.md) — `tests.yaml`
- [YAML Formats](reference/yaml.md) — complete schemas
