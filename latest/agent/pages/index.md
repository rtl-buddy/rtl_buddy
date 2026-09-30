---
description: RTL Buddy is a config-driven CLI for Verilog and SystemVerilog simulation, regressions, synthesis, formal verification, physical design, and debug workflows.
---

# RTL Buddy

RTL Buddy gives an RTL project one command surface for simulation, regressions, synthesis, formal verification, physical design, coverage, and debug tooling. The project describes models, tests, and flows in YAML; RTL Buddy resolves filelists, runs the external tools, records artefacts, and reports results the same way for local, CI, and agent runs.

## Supported workflows

- Simulation and randomized testing with Verilator, Icarus Verilog, or VCS
- Multi-suite regressions with local, local-parallel, or Slurm dispatch
- Yosys synthesis and OpenROAD timing, place-and-route, and power analysis
- SymbiYosys formal verification and mutation testing
- Verilator coverage collection, merge, query, and export
- Surfer waveform viewing and RTL hierarchy queries
- Verible linting, spec traceability, FPGA implementation, and AXI profiling
- Machine-readable output, a design knowledge graph, and an MCP server for agents

Each workflow needs its own external tools. See [Installation](install.md#external-tools-by-feature).

## Start here

- [Installation](install.md) — install RTL Buddy and the tools your workflow needs
- [Quick Start](quickstart.md) — run a test, regression, or synthesis entry in an existing project
- [Root Config](concepts/root-config.md) — configure platforms and tools
- [Tests](concepts/tests.md) — define and run a verification suite

## Reference

- [CLI Reference](reference/cli.md) — commands and options
- [YAML Formats](reference/yaml.md) — configuration schemas
