## Inspect artefacts

Outputs land under `<synth-dir>/artefacts/<run>/`.

| File | Backend | Purpose |
| --- | --- | --- |
| `synth.f`, `synth.ys` | Both | Resolved sources and generated Yosys script |
| `synth.rtlil` | Unmapped Yosys | Technology-independent netlist |
| `synth_netlist.v` | Mapped runs | Gate-level Verilog |
| `synth.log` | Yosys-only | Yosys output |
| `synth_yosys.log` | OpenROAD | First-stage Yosys output |
| `synth.tcl`, `synth.log` | OpenROAD | STA script and OpenROAD output |

When a run fails, inspect the relevant stage log first. Missing tools, plugin paths, Liberty, or LEF inputs are configuration failures; correct the path or installation and rerun the named synthesis.
