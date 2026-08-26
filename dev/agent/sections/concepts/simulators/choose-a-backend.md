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

Gate unsupported constructs when a suite must run on Icarus. Use [expected failures](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/expected-failures/) only when the failure mode is understood.
