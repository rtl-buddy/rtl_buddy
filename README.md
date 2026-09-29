# `rtl_buddy`

[![PyPI](https://img.shields.io/pypi/v/rtl_buddy)](https://pypi.org/project/rtl_buddy/)
[![Python](https://img.shields.io/pypi/pyversions/rtl_buddy)](https://pypi.org/project/rtl_buddy/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-rtl--buddy.github.io-blue)](https://rtl-buddy.github.io/rtl_buddy/)

`rtl_buddy` is a Python CLI for Verilog and SystemVerilog RTL design and verification. It drives the tools your project already uses from YAML configuration, with a consistent interface for humans, CI, and AI agents.

It covers simulation and randomized regressions, filelist generation, synthesis, place-and-route, power analysis, formal property verification, mutation testing, waveform viewing, hierarchy rendering, AXI profiling, and spec traceability.

```bash
uv run rb test basic
uv run rb test smoke --repeat 20
uv run rb regression
uv run rb regression --coverage-merge
uv run rb synth -c synth/sandbox/synth.yaml
uv run rb fpv -c fpv/sandbox/fpv.yaml
uv run rb wave basic
uv run rb axi-profile run basic
uv run rb tool-check
```

## Why `rtl_buddy`

- Run one test or a full regression from YAML instead of ad hoc shell scripts.
- Keep simulator invocation, seeds, logs, and result handling consistent across runs.
- Define filelists once in project models.
- Add sweep and preprocessing hooks without rewriting the main flow.
- Export JSONL logs and JSON results for CI and agent workflows.

## Features

- **Tests and regressions** (`rb test`, `rb randtest`, `rb regression`): one CLI across Verilator, Icarus Verilog, and VCS, with new-seed, repeat, and replay support. cocotb tests run through the same flow.
- **Configuration**: suites, regressions, platforms, builders, and models in YAML. `rb filelist` generates simulator-ready filelists from `models.yaml`.
- **Elaboration** (`rb elab`, `rb elab-regression`): parse, type-check, and elaborate `models.yaml` filelists with pyslang, without building a simulator.
- **Synthesis** (`rb synth`): Yosys from `synth.yaml`, with optional Liberty mapping, effort levels, synthesis regressions, and the yosys-slang frontend. OpenROAD is an alternative backend.
- **Place-and-route** (`rb pnr`): OpenROAD flow from the post-synthesis netlist to routed DEF, netlist, SDC, and timing/DRC reports.
- **Power analysis** (`rb power`, `rb power-regression`): OpenROAD `report_power` with static, synthetic, or SAIF/VCD activity. `rb saif` converts FST/VCD to SAIF.
- **Formal verification** (`rb fpv`, `rb fpv-regression`): SymbiYosys proofs with pinned solvers. `rb wave-fpv` opens a failed proof's counterexample.
- **Mutation testing** (`rb mut`): scores a verification suite by mutating a design and checking whether an FPV proof or simulation oracle kills each mutant. Uses the optional [rtl-buddy-xeno](https://github.com/rtl-buddy/rtl-buddy-xeno) engine.
- **Waveforms** (`rb wave`): opens [Surfer](https://surfer-project.org/) with live signal values annotated in your editor over WCP.
- **Hierarchy** (`rb hier`, `rb hier-query`): module hierarchy diagrams and queries through [rtl-buddy-view](https://github.com/rtl-buddy/rtl-buddy-view), with optional clock-domain annotations.
- **AXI profiling** (`rb axi-profile`): discover AXI bundles, emit a bind-style monitor, turn a test's FST into `axi-perf.json` and per-transaction Parquet, and open a marimo notebook.
- **Coordination hub** (`rb hub`): a broker between the rtl-buddy-view SPA, Surfer, and editor adapters. Serves graph (`/gph`), coverage (`/cov`), and physical-metrics (`/phy`) panes.
- **Physical metrics** (`rb phys`): per-module gate count and area, per-instance power, and a run listing from the merged synthesis and power model.
- **Spec traceability** (`rb spec`): trace `specs.yaml` items to models and tests.
- **Tool check** (`rb tool-check`): reports which `rb` subcommands are ready and which are blocked on missing or out-of-version tools.
- **Coverage**: collect, merge, summarize, and export Verilator coverage.
- **Hooks**: sweep generation and test preprocessing scripts.
- **Verible** (`rb verible`): lint, syntax, format, and preprocessor commands, plus `verible.filelist` generation.
- **Output for humans and machines**: Rich console output, and JSONL logs plus JSON results with `--machine`.

## Installation

`rtl_buddy` is on [PyPI](https://pypi.org/project/rtl_buddy/). It needs Python 3.11 or newer and `uv`:

```bash
uv add rtl_buddy
```

External tools depend on the commands you use. `rb test` needs a simulator, `rb synth` needs the [rtl-buddy/yosys fork](https://github.com/rtl-buddy/yosys), `rb pnr` and `rb power` need OpenROAD, `rb fpv` needs [SymbiYosys](https://github.com/YosysHQ/sby) and an SMT solver, `rb hier` needs [rtl-buddy-view](https://github.com/rtl-buddy/rtl-buddy-view), and `rb wave` needs the [rtl-buddy/surfer fork](https://github.com/rtl-buddy/surfer). The [installation page](https://rtl-buddy.github.io/rtl_buddy/latest/install/) has the full feature-to-tool matrix.

To develop `rtl_buddy` itself, install the `dev` group:

```bash
uv sync --group dev
npm ci                    # documentation builds only
uv run ruff check
uv run ruff format --check
uv run pytest
```

## Quick start

The [rtl-buddy project template](https://github.com/rtl-buddy/rtl-buddy-project-template) is a ready-to-run project with example designs, tests, and `rtl_buddy` integration. In a project:

```bash
uv run rb test basic      # run a single test
uv run rb regression      # run the full regression
uv run rb synth -c synth/sandbox/synth.yaml
```

See the [Quick Start guide](https://rtl-buddy.github.io/rtl_buddy/latest/quickstart/).

Artefacts go under `artefacts/{sanitized_test_name}/`. A single run writes `test.log`, `test.err`, `test.randseed`, and `coverage.dat` there. Repeated runs write to `artefacts/{sanitized_test_name}/run-0001/` and so on, and the three top-level `test.*` files are symlinks to the latest run.

## Documentation

Full documentation: **[rtl-buddy.github.io/rtl_buddy](https://rtl-buddy.github.io/rtl_buddy/)**. Each version also publishes `<version>/llms.txt` and `<version>/agent/catalog.json` for agents. Offline, use `rb docs list` and `rb docs show`.

Known limitations are on the [known issues page](https://rtl-buddy.github.io/rtl_buddy/latest/known-issues/).
