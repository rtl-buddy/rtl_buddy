---
description: Choose between Verilator and Icarus Verilog simulation backends and understand their coverage and waveform differences.
---

# Simulation Backends

Use Verilator by default. Use Icarus Verilog for lightweight smoke tests, or where installing Verilator is impractical.

## Choose a backend

| Capability | Verilator | Icarus 12 |
| --- | --- | --- |
| SystemVerilog procedural code | Yes | Yes |
| Concurrent SVA and `cover property` | Yes | No |
| `interface class` frameworks | Yes | No |
| RTL Buddy line/toggle coverage | Yes | No |
| cocotb through VPI | Yes | Yes |
| Default waveform | FST | VCD |

Verilator compiles a cycle-based `simv` binary. Icarus compiles a `.vvp` snapshot and runs it with `vvp`; RTL Buddy generates a `simv` wrapper so the rest of the flow is the same.

If a suite must run on Icarus, gate unsupported constructs. Use [expected failures](expected-failures.md) only when the failure mode is understood.

## Select a builder

The builder comes from, in order: `--builder <name>`, the test's `builder:`, the suite's `builder:`, the platform default.

Set `simulator-family` on the `cfg-rtl-builder` entry, or use an executable name RTL Buddy can infer it from. See [Selecting the simulator builder](../reference/yaml.md#selecting-the-simulator-builder).

## Open waveforms

Verilator writes `dump.fst` and Icarus writes `dump.vcd`. `rb wave` opens the newest supported dump under `artefacts/<test>/`.

To convert an Icarus dump to FST after simulation, set `wave-format: fst-postproc` on the builder. If `vcd2fst` is missing, the VCD is kept.

## Collect coverage

RTL Buddy coverage supports Verilator only and follows the platform-selected builder. Pass `--builder verilator` or make Verilator the platform default; a suite- or test-level override alone is not enough. See [Coverage uses the platform builder](../known-issues.md#coverage-uses-the-platform-builder).
