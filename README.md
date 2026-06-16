# `rtl_buddy`

[![PyPI](https://img.shields.io/pypi/v/rtl_buddy)](https://pypi.org/project/rtl_buddy/)
[![Python](https://img.shields.io/pypi/pyversions/rtl_buddy)](https://pypi.org/project/rtl_buddy/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-rtl--buddy.github.io-blue)](https://rtl-buddy.github.io/rtl_buddy/)

`rtl_buddy` is a Python CLI for Verilog and SystemVerilog RTL design and verification workflows: simulator-driven tests and randomized regressions, filelist generation, synthesis, place-and-route, power analysis, CDC lint, formal property verification, mutation testing, waveform viewing, hierarchy rendering, AXI interconnect profiling, spec traceability, and adjacent automation. It is designed to work well for both humans and AI agents.

It is built to sit on top of the tools your project already uses, while giving you a cleaner, more repeatable interface for day-to-day RTL work. Current first-class flows cover Verilator/VCS simulation (with optional cocotb), Yosys synthesis (with optional yosys-slang frontend), OpenROAD-based place-and-route and power analysis, [rtl-buddy-cdc](https://github.com/rtl-buddy/rtl-buddy-cdc) CDC lint, SymbiYosys formal verification, and Surfer-based waveform viewing with live editor annotation. Verible command integration covers lint, syntax, format, preprocessor, and `verible.filelist` generation; broader first-class Verible and PeakRDL workflows are on the roadmap.

Typical commands look like:

```bash
uv run rb test basic
uv run rb test smoke --repeat 20
uv run rb regression
uv run rb regression --coverage-merge
uv run rb synth -c synth/sandbox/synth.yaml
uv run rb cdc -c cdc/sandbox/cdc.yaml
uv run rb fpv -c fpv/sandbox/fpv.yaml
uv run rb wave basic
uv run rb axi-profile run basic
uv run rb tool-check
```

## Why `rtl_buddy`

`rtl_buddy` gives RTL projects a lightweight control plane for common verification tasks:

- Run a single test or a full regression from YAML config instead of ad hoc shell scripts
- Keep simulator invocation, seeds, logs, and result handling consistent across runs
- Manage filelists easily with project model definitions
- Add sweep generation, preprocessing, and postprocessing hooks without rewriting the main flow
- Export machine-readable logs that work well in CI and AI-agent-driven workflows

## Features

- **Test and regression commands**: run one test, many tests, or whole suites with a consistent CLI across Verilator and VCS
- **Randomized testing support**: create new seeds, repeat runs, and replay previous randomized iterations
- **Structured config model**: describe suites, regressions, platforms, builders, and models in readable YAML
- **Filelist generation**: build simulator-ready filelists from `models.yaml`
- **Synthesis flows** (`rb synth`): run Yosys synthesis from `synth.yaml`, including optional Liberty-mapped runs, synthesis regressions, configurable effort levels, and an optional yosys-slang frontend; OpenROAD is also available as an alternative backend
- **Place-and-route** (`rb pnr`): OpenROAD-driven flow that consumes the post-synth netlist and produces routed DEF, post-route netlist + SDC, and timing/DRC reports
- **Power analysis** (`rb power`, `rb power-regression`): OpenROAD `report_power` over post-synth or post-PnR netlists, with static, synthetic, or SAIF/VCD activity sources (`rb saif` converts FST/VCD traces to SAIF v2.0)
- **CDC lint** (`rb cdc`, `rb cdc-regression`): first-class integration with [rtl-buddy-cdc](https://github.com/rtl-buddy/rtl-buddy-cdc)
- **Formal property verification** (`rb fpv`, `rb fpv-regression`): SymbiYosys-driven proofs with reproducible solver pinning; `rb wave-fpv` opens the counterexample VCD for a failed run
- **Mutation testing** (`rb mut`): scores how well a verification suite catches injected bugs by mutating a design file and checking whether an FPV proof or a simulation/assertion oracle kills each mutant (via the optional [rtl-buddy-xeno](https://github.com/rtl-buddy/rtl-buddy-xeno) engine)
- **Waveform viewing** (`rb wave`): opens [Surfer](https://surfer-project.org/) with live signal-value annotation in your editor via the WCP protocol
- **Hierarchy rendering** (`rb hier`): module hierarchy diagrams via [rtl-buddy-view](https://github.com/rtl-buddy/rtl-buddy-view), with optional CDC and RDC clock-domain annotations
- **AXI interconnect profiling** (`rb axi-profile`): discover AXI bundles from RTL, emit a bind-style SV monitor, ingest a test's FST into per-test `axi-perf.json` + per-transaction Parquet, and launch a packaged marimo notebook for interactive analysis
- **Coordination hub** (`rb hub`): TCP + HTTP/WebSocket broker that mediates between the rtl-buddy-view SPA, Surfer (via `rb wave`), and editor adapters; supports runtime model switching, AXI-perf overlays, and CDC diagnostics; optional macOS LaunchAgent install
- **Spec traceability** (`rb spec`): trace `specs.yaml` items to design models (`check-design`) and tests (`check-coverage`)
- **Tool dependency check** (`rb tool-check`): declarative manifest of external tool dependencies — reports which `rb` subcommands are ready and which are blocked on missing or out-of-version tools
- **Coverage workflows**: collect, merge, summarize, and export Verilator coverage
- **cocotb support**: Verilator + VPI cocotb tests integrated into the standard test/regression flow
- **Hookable execution flow**: plug in your own sweep generation, test preprocessing, and postprocessing scripts
- **Verible integration** (`rb verible`): invoke lint, syntax, formatting, and preprocessor commands through the same project config, plus generate `verible.filelist` from `models.yaml` for `verible-verilog-ls`
- **Rich outputs for humans**: displays pretty formatted for easy reading
- **Structured logging for machines**: emits JSONL logs for interpretation by CI systems, automation, and coding agents
- **Cross-project reuse**: keep one tool interface while adapting it to different RTL repo layouts and builder setups

## Installation

`rtl_buddy` is available on [PyPI](https://pypi.org/project/rtl_buddy/) and installed into your project environment with `uv`:

```bash
uv add rtl_buddy
```

For local development in this repo, install the composite `dev` group:

```bash
uv sync --group dev
uv run ruff check
uv run ruff format --check
uv run pytest
```

Prerequisites:

- Python 3.11+
- `uv`

Beyond Python and `uv`, every other dependency is feature-dependent: which external tools you need depends on which `rb` commands you use. For example, `rb test` needs a simulator (Verilator / VCS), `rb synth` needs the [rtl-buddy/yosys fork](https://github.com/rtl-buddy/yosys), `rb pnr` and `rb power` need OpenROAD, `rb cdc` needs [rtl-buddy-cdc](https://github.com/rtl-buddy/rtl-buddy-cdc), `rb fpv` needs [SymbiYosys](https://github.com/YosysHQ/sby) plus an SMT solver, `rb hier` needs [rtl-buddy-view](https://github.com/rtl-buddy/rtl-buddy-view), and `rb wave` needs the [rtl-buddy/surfer fork](https://github.com/rtl-buddy/surfer).

See the [installation page](https://rtl-buddy.github.io/rtl_buddy/latest/install/) for the full feature-to-dependency matrix, including integration types (Integrated tool vs Pluggable vs Pluggable — curated) and install commands.

## Documentation

Full documentation is at **[rtl-buddy.github.io/rtl_buddy](https://rtl-buddy.github.io/rtl_buddy/)**.

## Quick Start

The fastest way to get started is the **[rtl-buddy project template](https://github.com/rtl-buddy/rtl-buddy-project-template)** — a ready-to-run RTL project with example designs, tests, and full `rtl_buddy` integration.

Once you have a project set up, the basic commands are:

```bash
uv run rb test basic      # run a single test
uv run rb regression      # run the full regression
uv run rb synth -c synth/sandbox/synth.yaml
```

For full usage, see the [Quick Start guide](https://rtl-buddy.github.io/rtl_buddy/latest/quickstart/).

Runtime artefacts are stored under `artefacts/{sanitized_test_name}/`. Single runs write files such as `test.log`, `test.err`, `test.randseed`, and `coverage.dat` there directly, while repeated runs use nested directories such as `artefacts/{sanitized_test_name}/run-0001/`. The suite root always keeps `test.log`, `test.err`, and `test.randseed` symlinked to the latest run for convenience.

## Known Issues

See the [known issues page](https://rtl-buddy.github.io/rtl_buddy/latest/known-issues/).
