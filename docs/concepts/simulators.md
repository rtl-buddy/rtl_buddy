---
description: Verilator vs Icarus Verilog as rtl_buddy simulation backends — the capability split, when to use each, and the coverage limitation.
---

# Simulators (Verilator vs Icarus)

`rtl_buddy` drives a Verilog/SystemVerilog simulation through a named builder in `root_config.yaml`'s `cfg-rtl-builder`. Two open-source backends are supported today: [Verilator](https://www.veripool.org/verilator/) (the default) and [Icarus Verilog](https://steveicarus.github.io/iverilog/). They differ in language support, speed, and waveform format, so the right choice depends on the test. This page covers the split and how a builder is selected; the YAML mechanics live in [Selecting the simulator builder](../reference/yaml.md#selecting-the-simulator-builder).

## Choosing a backend

Use **Verilator** as the default. It is a cycle-accurate compiled simulator: fast, with broad SystemVerilog-2017 support including SVA `assert`/`cover property`, functional coverage hooks, and the line/toggle coverage that feeds `rb test --coverage`. It is the backend the regression tiers and CI run on.

Reach for **Icarus** when you want a lightweight, quick-to-install event-driven simulator (`brew install icarus-verilog` / `apt install iverilog`) — useful for small smoke tests, portable demos, and environments where building Verilator is inconvenient. Icarus 12 supports SystemVerilog-2012 procedural code well but **does not** support `cover property` SVA, `interface class`-based frameworks, or coverage collection through rtl_buddy's pipeline. Designs and testbenches that rely on those constructs must gate them out for Icarus (see [Expected failures](expected-failures.md) and the `SIM_ICARUS` gating pattern in the project template's `demo_tiny_alu`).

## Capability split

| Capability | Verilator | Icarus 12 |
|------------|-----------|-----------|
| SV-2012 procedural code | Yes | Yes |
| `cover property` / concurrent SVA | Yes | No |
| `interface class` frameworks | Yes | No |
| Line/toggle coverage (`rb test --coverage`) | Yes | No |
| cocotb (Python TB via VPI) | Yes | Yes |
| Default waveform dump | FST (`dump.fst`) | VCD (`dump.vcd`) |
| Simulation model | Compiled, cycle-based | Interpreted, event-driven |

## How the backend is selected

A builder is resolved with this precedence (highest first): the `--builder <name>` CLI override, a per-test `builder:`, a suite-wide `builder:`, then the platform default from `cfg-platforms`. The chosen builder's `simulator-family` — explicit in `cfg-rtl-builder` or inferred from the executable name (`iverilog`→`icarus`, `verilator`→`verilator`) — is what drives every backend-specific decision: the two-phase `iverilog`→`vvp` compile/run flow, assertion flags, and coverage handling. See [Selecting the simulator builder](../reference/yaml.md#selecting-the-simulator-builder) for the full YAML.

## Compile and run flow

Verilator compiles the design to a single `simv` binary (`verilator --binary`) that rtl_buddy runs directly. Icarus is two-phase: `iverilog -o <snapshot>.vvp` compiles to a `.vvp` snapshot, then `vvp <snapshot>` interprets it. rtl_buddy hides this by writing a small `simv` shell wrapper that execs `vvp` on the snapshot, so the runner invokes one executable regardless of backend. For cocotb on Icarus the wrapper additionally loads the cocotb VPI module (`vvp -M <cocotb-libs> -m libcocotbvpi_icarus`), since Icarus binds VPI at run time rather than at compile.

## Waveforms

Verilator dumps FST (`dump.fst`); Icarus dumps plain VCD (`dump.vcd`). `rb wave` discovers whichever dump is newest under `artefacts/<test>/` — so the Icarus VCD opens in Surfer without conversion, as Surfer reads VCD natively. To force an FST instead (for tooling that needs it), set `wave-format: fst-postproc` on the Icarus builder; rtl_buddy then runs `vcd2fst` (GTKWave) after the sim, falling back to the VCD if `vcd2fst` is not installed.

## Coverage limitation

Coverage collection and reporting key off the **platform-selected** builder, not a per-test/suite `builder:`. Because only Verilator emits line/toggle coverage today, running an Icarus test for coverage through a Verilator-default platform can mislabel or miss results. Run the suite with `--builder` (or make the builder the platform default) to keep simulation and coverage on the same backend. See [Coverage follows the platform builder](../known-issues.md#coverage-follows-the-platform-builder-not-a-per-testsuite-builder) for the full rationale.
