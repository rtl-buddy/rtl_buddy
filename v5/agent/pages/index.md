---
description: RTL Buddy is a Python CLI for Verilog and SystemVerilog regression testing, Verilator and VCS simulation workflows, Yosys synthesis flows, coverage, and YAML-based RTL automation.
---

# RTL Buddy

RTL Buddy is a Python CLI for Verilog and SystemVerilog RTL workflows, including test execution, randomized regression testing, Verilator and VCS simulation, Yosys synthesis, coverage collection, and YAML-based automation.

It wraps simulation, synthesis, and project scripts to provide a structured, config-driven test, regression, and synthesis system for ASIC and FPGA projects. The primary supported simulation flows are Verilator on macOS/Linux and VCS on Linux.

## Features

- Run individual Verilog/SystemVerilog tests or full regressions from YAML config files
- Randomized seed testing with repeat and replay support
- Plugin hooks for sweep generation, test pre-processing, and post-processing
- Filelist generation from `models.yaml`
- Yosys synthesis (`rb synth`) with optional Liberty mapping, a yosys-slang frontend, and an OpenROAD backend
- OpenROAD place-and-route (`rb pnr`) and gate-level power analysis (`rb power`, with `rb saif` activity capture)
- CDC lint (`rb cdc`) via [rtl-buddy-cdc](https://github.com/rtl-buddy/rtl-buddy-cdc)
- Formal property verification (`rb fpv`) with SymbiYosys, solver pinning, and `rb wave-fpv` counterexample viewing
- Mutation testing (`rb mut`) that scores a verification suite by mutating a design and checking whether an FPV or simulation/assertion oracle kills each mutant
- Waveform viewing (`rb wave`) in [Surfer](https://surfer-project.org/) with live editor annotation, and module hierarchy rendering (`rb hier`)
- AXI interconnect profiling (`rb axi-profile`) with a packaged marimo notebook, and a coordination hub (`rb hub`) tying the view SPA, Surfer, and editors together
- Spec traceability (`rb spec`) and a declarative external-tool readiness check (`rb tool-check`)
- Verilator coverage collection, merge, summary, and export workflows
- Verible command integration (`rb verible`) for lint, syntax, formatting, preprocessing, and `verible.filelist` generation
- Machine-readable JSONL logging for use with AI agents and CI pipelines

`rtl_buddy` can be adapted to different project toolchains, but the primary supported simulation flows are Verilator and VCS, and synthesis through Yosys. Broader first-class Verible and PeakRDL workflows are on the roadmap.

## SystemVerilog Regression Testing

RTL Buddy keeps simulator invocation, seeds, logs, result handling, and regression selection consistent across repeated verification runs. Projects describe suites, tests, platforms, builders, and models in readable YAML files instead of scattering that logic across ad hoc shell scripts.

## Verilator and VCS Simulation Workflows

The CLI gives RTL projects one command surface for open-source Verilator flows and Linux VCS flows, with optional hooks for project-specific compile, simulation, sweep, and post-processing behavior.

## Yosys Synthesis Workflows

Synthesis configs use the same YAML-driven style as tests and regressions. Projects can define technology-independent or Liberty-mapped Yosys runs, capture synthesis artefacts, and run synthesis regressions from one command surface.

## Getting Started

- [Installation](install.md) — how to add `rtl_buddy` to your project
- [Quick Start](quickstart.md) — run your first test in minutes
- [Concepts](concepts/root-config.md) — understand the config model

## Reference

- [CLI Reference](reference/cli.md) — all subcommands and options
- [YAML Formats](reference/yaml.md) — full schema for all config files
