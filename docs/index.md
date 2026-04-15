# RTL Buddy

RTL Buddy is a CLI tool for automating tests, regressions, and RTL workflows for Verilog/SystemVerilog codebases.

It wraps simulation tools (primarily Verilator on macOS/Linux and VCS on Linux) and provides a structured, config-driven test and regression system for ASIC and FPGA projects.

## Features

- Run individual tests or full regressions from YAML config files
- Randomized seed testing with repeat support
- Plugin hooks for sweep generation, test pre-processing, and post-processing
- Filelist generation from `models.yaml`
- Basic Verible command integration for lint, syntax, formatting, and preprocessing
- Machine-readable JSONL logging for use with AI agents and CI pipelines

`rtl_buddy` can be adapted to different project toolchains, but the primary supported flows are Verilator and VCS. Broader first-class Verible and PeakRDL workflows are on the roadmap.

## Getting Started

- [Installation](install.md) — how to add `rtl_buddy` to your project
- [Quick Start](quickstart.md) — run your first test in minutes
- [Concepts](concepts/root-config.md) — understand the config model

## Reference

- [CLI Reference](reference/cli.md) — all subcommands and options
- [YAML Formats](reference/yaml.md) — full schema for all config files
