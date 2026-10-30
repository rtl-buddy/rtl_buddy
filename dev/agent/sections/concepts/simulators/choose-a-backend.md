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

xezim parses, elaborates and simulates in one invocation. The compile runs `xezim --compile`, which stops after elaboration, so syntax and elaboration errors fail the compile as with the other backends. RTL Buddy then writes a `simv` wrapper that reruns the same command line without `--compile`. Both point `--cache-dir` at the build directory, so runs reuse the elaborated design. The wrapper also adds `--error-exit`: without it xezim exits 0 after `$error` or a failed assertion, so the exit status could not fail a test, with or without a PASS/FAIL marker. Failed assertions print `** Error: Assertion error.`, which `assertions: true` counts. Note that:

- `$dumpfile`/`$dumpvars` do nothing without `--wave`. Put `--wave`, or `--fst dump.fst`, in the builder's `compile-time` options for the modes that need waves.
- xezim stops at `--max-time`, 100 ms of simulated time by default. Raise it in `compile-time` for longer tests.
- xezim is built from source: `cargo build --release` in a [xezim](https://github.com/aionhw/xezim) checkout (Rust 1.92+). `rb tool-check` lists it as optional.

If a suite must run on Icarus, gate unsupported constructs. Use [expected failures](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/expected-failures/) only when the failure mode is understood.
