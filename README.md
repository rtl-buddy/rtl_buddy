# `rtl_buddy`

`rtl_buddy` is a CLI for running RTL tests, regressions, filelist generation, and adjacent workflow automation in Verilog and SystemVerilog projects. It is designed to work well for both humans and AI agents.

It is built to sit on top of the tools your project already uses, while giving you a cleaner, more repeatable interface for day-to-day verification work. The primary supported flows are Verilator and VCS-based compile, simulation, and regression workflows. Basic Verible command integration exists, while broader first-class Verible and PeakRDL workflows are on the roadmap.

## Why `rtl_buddy`

`rtl_buddy` gives RTL projects a lightweight control plane for common verification tasks:

- Run a single test or a full regression from YAML config instead of ad hoc shell scripts
- Keep simulator invocation, seeds, logs, and result handling consistent across runs
- Manage filelists easily with project model definitions
- Add sweep generation, preprocessing, and postprocessing hooks without rewriting the main flow
- Export machine-readable logs that work well in CI and AI-agent-driven workflows

## Features

- **Test and regression commands**: run one test, many tests, or whole suites with a consistent CLI
- **Randomized testing support**: create new seeds, repeat runs, and replay previous randomized iterations
- **Structured config model**: describe suites, regressions, platforms, builders, and models in readable YAML
- **Filelist generation**: build simulator-ready filelists from `models.yaml`
- **Coverage workflows**: collect, merge, summarize, and export Verilator coverage
- **Hookable execution flow**: plug in your own sweep generation, test preprocessing, and postprocessing scripts
- **Verible integration**: invoke lint, syntax, formatting, and preprocessing commands through the same project config
- **Rich outputs for humans**: displays pretty formatted for easy reading
- **Structured logging for machines**: emits JSONL logs for interpretation by CI systems, automation, and coding agents
- **Cross-project reuse**: keep one tool interface while adapting it to different RTL repo layouts and builder setups

## Installation

`rtl_buddy` is installed into your project environment with `uv` directly from the Git repository. PyPI publication is planned but not yet available.

Prerequisites:

- Python 3.11+
- `uv`
- A simulator on `PATH`
  - Verilator is the recommended open-source starting point
  - VCS is also supported as a first-class flow
- Optional Verible binaries if you want to use `uv run rb verible ...`
- Optional system-level coverage tools:
  - `lcov` for LCOV and HTML coverage export
  - [Coverview](https://github.com/antmicro/coverview) for Coverview package generation

See [docs/install.md](docs/install.md) for the full install flow.

## Documentation

Full documentation lives in [`docs/`](docs/), is built with MkDocs, and is intended to be published on GitHub Pages.

To preview the docs locally while developing:

```bash
uv sync --group docs
uv run mkdocs serve
```

Useful entry points:

- [Installation](docs/install.md)
- [Quick Start](docs/quickstart.md)
- [Coverage](docs/concepts/coverage.md)
- [CLI Reference](docs/reference/cli.md)
- [YAML Formats](docs/reference/yaml.md)

## Quick Start

Run a test:

```bash
uv run rb test basic
```

Run a regression:

```bash
uv run rb regression
```

For full usage, see [docs/quickstart.md](docs/quickstart.md).

## Known Issues

See [docs/known-issues.md](docs/known-issues.md).
