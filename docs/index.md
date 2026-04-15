# RTL Buddy

RTL Buddy is a CLI tool for automating tests, regressions, and RTL workflows for Verilog/SystemVerilog codebases.

It wraps simulation tools (Verilator on macOS, VCS on Linux) and provides a structured, config-driven test and regression system for ASIC and FPGA projects.

## Features

- Run individual tests or full regressions from YAML config files
- Randomized seed testing with repeat support
- Plugin hooks for sweep generation, test pre-processing, and post-processing
- Filelist generation from `models.yaml`
- Verible lint and syntax checking integration
- Machine-readable JSONL logging for use with AI agents and CI pipelines

## Getting Started

- [Installation](install.md) — how to add `rtl_buddy` to your project
- [Quick Start](quickstart.md) — run your first test in minutes
- [Concepts](concepts/root-config.md) — understand the config model

## Reference

- [CLI Reference](reference/cli.md) — all subcommands and options
- [YAML Formats](reference/yaml.md) — full schema for all config files
