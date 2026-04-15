# Quick Start

This guide assumes `rtl_buddy` is already installed in your project. See [Installation](install.md) if not.

## Run a test

Run the test named `basic` using `tests.yaml` in the current directory:

```bash
rtl-buddy test basic
```

Specify a different test config file:

```bash
rtl-buddy test basic --test-config path/to/tests.yaml
```

Run all tests in a config:

```bash
rtl-buddy test
```

List available tests without running them:

```bash
rtl-buddy test --list
```

## Run a regression

```bash
rtl-buddy regression
```

This uses the regression config path from `root_config.yaml`. To specify a different file:

```bash
rtl-buddy regression --reg-config path/to/regressions.yaml
```

## Run with randomization

Run a test once with a new random seed:

```bash
rtl-buddy test basic --rnd-new
```

Run the same test 5 times with different seeds:

```bash
rtl-buddy randtest basic 5
```

Repeat a specific iteration from a previous `randtest` run:

```bash
rtl-buddy randtest basic 5 --rnd-rpt 3
```

## Check logs

`rtl_buddy` writes orchestration logs to `rtl_buddy.log` in the directory where it is run.

Simulation output for each test goes to `logs/{test_name}.log`. For convenience, the symlinks `test.log`, `test.err`, and `test.randseed` always point to the latest run.

For machine-readable output (useful with CI or AI agents):

```bash
rtl-buddy --machine test basic
```

In machine mode, `rtl_buddy.log` is written as JSON Lines and console output is plain text. See [For Agents](agents.md) for more on machine mode.

## Next steps

- [Concepts: Tests](concepts/tests.md) — understand the test config model
- [Concepts: Regressions](concepts/regressions.md) — run multi-suite regressions
- [YAML Formats](reference/yaml.md) — full config file reference
