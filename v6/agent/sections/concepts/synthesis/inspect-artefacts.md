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

Both netlists are deleted at the very start of each run, before the filelist is even generated and before Yosys is looked for at all, so every way a run can fail leaves them absent — there is no missing-tool carve-out here, because `rb pnr` and `rb power` resolve the netlist by path and must never be handed the previous run's. A run that fails publishes nothing. Yosys writes the netlist partway through its script and only then runs the trailing `stat`, so it can crash — or log an `ERROR:` line — with the netlist already on disk; and on the OpenROAD backend the Yosys stage can succeed before the timing stage fails. Every one of those paths removes the netlist again, so a `FAIL` never leaves a design for `rb pnr` or `rb power` to pick up. They are the fixed-path inputs `rb pnr` and `rb power` resolve, so a failed rerun that left the last successful run's netlist in place would have those commands place, route, and power-analyse a design that is no longer what the RTL says. A failed run therefore leaves no netlist at all, and `rb pnr` reports that you need to run `rb synth` first. Copy a netlist you want to compare against out of the artefact directory before rerunning.

When a run fails, inspect the relevant stage log first. Missing tools, plugin paths, Liberty, or LEF inputs are configuration failures; correct the path or installation and rerun the named synthesis.
