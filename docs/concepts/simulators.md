---
description: Choose between Verilator, Icarus Verilog and xezim simulation backends and understand their coverage and waveform differences.
---

# Simulation Backends

Use Verilator by default. Use Icarus Verilog for lightweight smoke tests, or where installing Verilator is impractical. Use xezim for a 4-state simulator that checks concurrent SVA without a C++ build step.

## Choose a backend

| Capability | Verilator | Icarus 12 | xezim |
| --- | --- | --- | --- |
| SystemVerilog procedural code | Yes | Yes | Yes |
| Concurrent SVA and `cover property` | Yes | No | Yes |
| `interface class` frameworks | Yes | No | Yes |
| RTL Buddy line/toggle coverage | Yes | No | Yes (statement-level) |
| cocotb through VPI | Yes | Yes | Yes (VCS-flavour VPI library) |
| Default waveform | FST | VCD | FST, with `--fst` |

Verilator compiles a cycle-based `simv` binary. Icarus compiles a `.vvp` snapshot and runs it with `vvp`; RTL Buddy generates a `simv` wrapper so the rest of the flow is the same.

xezim parses, elaborates and simulates in one invocation. The compile runs `xezim --compile`, which stops after elaboration, so syntax and elaboration errors fail the compile as with the other backends. RTL Buddy then writes a `simv` wrapper that reruns the same command line without `--compile`. Both point `--cache-dir` at the build directory, so runs reuse the elaborated design. The wrapper also adds `--error-exit`: without it xezim exits 0 after `$error` or a failed assertion, and a test with no PASS/FAIL marker could not be graded. Failed assertions print `** Error: Assertion error.`, which `assertions: true` counts. Note that:

- `$dumpfile`/`$dumpvars` do nothing without `--wave`. Put `--wave`, or `--fst dump.fst`, in the builder's `compile-time` options for the modes that need waves.
- xezim stops at `--max-time`, 100 ms of simulated time by default. Raise it in `compile-time` for longer tests.
- xezim is built from source: `cargo build --release` in a [xezim](https://github.com/aionhw/xezim) checkout (Rust 1.92+). `rb tool-check` lists it as optional.

If a suite must run on Icarus, gate unsupported constructs. Use [expected failures](expected-failures.md) only when the failure mode is understood.

## Select a builder

The builder comes from, in order: `--builder <name>`, the test's `builder:`, the suite's `builder:`, the platform default.

Set `simulator-family` on the `cfg-rtl-builder` entry, or use an executable name RTL Buddy can infer it from. See [Selecting the simulator builder](../reference/yaml.md#selecting-the-simulator-builder).

## Open waveforms

Verilator writes `dump.fst`, Icarus writes `dump.vcd`, and xezim writes the file `--fst` names. `rb wave` opens the newest supported dump under `artefacts/<test>/`.

To convert an Icarus dump to FST after simulation, set `wave-format: fst-postproc` on the builder. If `vcd2fst` is missing, the VCD is kept.

## Collect coverage

RTL Buddy coverage supports Verilator and xezim and follows the platform-selected builder. Pass `--builder <name>` or make that builder the platform default; a suite- or test-level override alone is not enough. See [Coverage uses the platform builder](../known-issues.md#coverage-uses-the-platform-builder). xezim coverage is statement-level and has no expression metric; see [Collect coverage with xezim](coverage.md#collect-coverage-with-xezim).
