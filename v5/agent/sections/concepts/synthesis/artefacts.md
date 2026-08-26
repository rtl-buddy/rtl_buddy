## Artefacts

Synthesis artefacts land under `artefacts/<synth_name>/` relative to the `synth.yaml` directory.

**Yosys backend:**

| File | Contents |
|------|----------|
| `synth.f` | Generated source filelist (resolved from `models.yaml`) |
| `synth.ys` | Generated Yosys script |
| `synth.log` | Captured Yosys stdout and stderr |
| `synth.rtlil` | Output netlist, technology-independent flow (RTLIL format) |
| `synth_netlist.v` | Output netlist, technology-mapped flow (Verilog) |

**OpenROAD backend:**

| File | Contents |
|------|----------|
| `synth.f` | Generated source filelist |
| `synth.ys` | Yosys script (stage 1 — maps RTL to gate-level netlist) |
| `synth_yosys.log` | Yosys stdout and stderr |
| `synth_netlist.v` | Gate-level Verilog produced by Yosys, fed into OpenROAD |
| `synth.tcl` | OpenROAD Tcl script (stage 2 — timing analysis) |
| `synth.log` | OpenROAD stdout and stderr |
