---
description: Choose between Verilator and Icarus Verilog simulation backends and understand their coverage and waveform differences.
---

# Simulation Backends

Use Verilator by default. Use Icarus Verilog for lightweight smoke tests or environments where installing Verilator is impractical.

## Choose a backend

| Capability | Verilator | Icarus 12 |
| --- | --- | --- |
| SystemVerilog procedural code | Yes | Yes |
| Concurrent SVA and `cover property` | Yes | No |
| `interface class` frameworks | Yes | No |
| RTL Buddy line/toggle coverage | Yes | No |
| cocotb through VPI | Yes | Yes |
| Default waveform | FST | VCD |

Verilator compiles a cycle-based `simv` binary and provides RTL Buddy's coverage path. Icarus compiles a `.vvp` snapshot and runs it through `vvp`; RTL Buddy generates a `simv` wrapper so the surrounding flow stays the same.

Gate unsupported constructs when a suite must run on Icarus. Use [expected failures](expected-failures.md) only when the failure mode is understood.

## Select a builder

Builder precedence is `--builder <name>`, per-test `builder:`, suite-level `builder:`, then the platform default.

The selected `cfg-rtl-builder` entry should set `simulator-family`, or use an executable name from which RTL Buddy can infer it. See [Selecting the simulator builder](../reference/yaml.md#selecting-the-simulator-builder).

## Open waveforms

Verilator normally writes `dump.fst`; Icarus writes `dump.vcd`. `rb wave` opens the newest supported dump under `artefacts/<test>/`.

Set `wave-format: fst-postproc` on an Icarus builder to run `vcd2fst` after simulation. If `vcd2fst` is unavailable, the VCD remains usable.

## Collect coverage

RTL Buddy coverage is currently Verilator-only and follows the platform-selected builder. Use `--builder verilator`, or make Verilator the platform default, instead of relying only on a suite- or test-level override. See [Coverage uses the platform builder](../known-issues.md#coverage-uses-the-platform-builder).
