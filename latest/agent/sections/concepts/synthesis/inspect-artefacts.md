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
| `yosys-tmp/` | Both | Yosys `TMPDIR`: temp files and the merged Liberty SCL cache that `abc` builds from several cell libraries. It replaces any inherited `TMPDIR`. |
| `synth_stat.json` | Both | Yosys `stat -json`: per-module cell count and area |
| `phys-model.json`, `phys-manifest.json` | Both | Physical model and its manifest; query with `rb phys` ([Physical Metrics](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/phys/)) |

A failed run leaves no netlist. Netlists are deleted at the start of every run, so `rb pnr` and `rb power` never consume a previous run's design and report that `rb synth` must run first. Copy out a netlist before rerunning if you want to keep it.

A power run for the same top in the same artefact directory fills the model's per-instance half; see [Pair the model with a synthesis run](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/power/#pair-the-model-with-a-synthesis-run). A re-synthesis keeps those rows only when its netlist is byte-identical to the one the power run read, so after an RTL edit rerun `rb power`.
