# `rtl_buddy`

[![PyPI](https://img.shields.io/pypi/v/rtl_buddy)](https://pypi.org/project/rtl_buddy/)
[![Python](https://img.shields.io/pypi/pyversions/rtl_buddy)](https://pypi.org/project/rtl_buddy/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-rtl--buddy.github.io-blue)](https://rtl-buddy.github.io/rtl_buddy/)

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

`rtl_buddy` is available on [PyPI](https://pypi.org/project/rtl_buddy/) and installed into your project environment with `uv`:

```bash
uv add rtl_buddy
```

Prerequisites:

- Python 3.11+
- `uv`
- A simulator on `PATH`
  - Verilator is the recommended open-source starting point
  - VCS is also supported as a first-class flow
- Optional: Verible if you want to use `uv run rb verible ...` — e.g. `brew tap chipsalliance/verible && brew install verible` on macOS, or see [Verible releases](https://github.com/chipsalliance/verible/releases) for other platforms
- Optional system-level coverage tools:
  - `lcov` for LCOV and HTML coverage export
  - [Coverview](https://github.com/antmicro/coverview) for Coverview package generation

## Documentation

Full documentation is at **[rtl-buddy.github.io/rtl_buddy](https://rtl-buddy.github.io/rtl_buddy/)**.

## Quick Start

The fastest way to get started is the **[rtl-buddy project template](https://github.com/rtl-buddy/rtl-buddy-project-template)** — a ready-to-run RTL project with example designs, tests, and full `rtl_buddy` integration.

Once you have a project set up, the basic commands are:

```bash
uv run rb test basic      # run a single test
uv run rb regression      # run the full regression
```

For full usage, see the [Quick Start guide](https://rtl-buddy.github.io/rtl_buddy/quickstart/).

## Known Issues

See the [known issues page](https://rtl-buddy.github.io/rtl_buddy/known-issues/).
