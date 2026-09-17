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
| `synth_stat.json` | Both | Yosys `stat -json`: per-module cell count and area |
| `phys-model.json` | Both | Physical model — per-module rows plus the design totals |
| `phys-manifest.json` | Both | Which physical artefacts this run produced, and where |
| `phys-publish.lock` | Both | Mutex held while that pair is rewritten; empty between publishes |

Query the model with `rb phys`; see [Physical Metrics](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/phys/).

The physical model is a by-product, never a gate: a run that produced a netlist has passed whether or not Yosys also wrote a readable `stat -json`. When the per-module dump is missing or unreadable, the model still records the design totals and its `modules` block is `null` — a warning says so, and the synthesis still reports `PASS`. A `rb power` run publishing into the same artefact directory for the same top fills the model's per-instance half rather than replacing it — its own by default, or this one when its `phys-run:` names this synthesis; see [Pair the model with a synthesis run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/#pair-the-model-with-a-synthesis-run). A re-synthesis carries those per-instance rows forward only when the netlist it has just written is byte-identical to the one the power run read — both halves record that netlist's hash, and the same check runs in the other direction when it is `rb power` that publishes second. Edit the RTL and re-run `rb synth` and the rows are dropped (`instances: null`, with their totals) rather than left standing against a netlist that no longer exists; re-run `rb power` to measure the new one. A synthesis and a power run publishing into one directory at the same moment take `phys-publish.lock` in turn, so the pair they leave carries both halves whichever finishes first.

Both netlists are deleted at the very start of each run, before the filelist is even generated and before Yosys is looked for at all, so every way a run can fail leaves them absent — there is no missing-tool carve-out here, because `rb pnr` and `rb power` resolve the netlist by path and must never be handed the previous run's. A run that fails publishes nothing. Yosys writes the netlist partway through its script and only then runs the trailing `stat`, so it can crash — or log an `ERROR:` line — with the netlist already on disk; and on the OpenROAD backend the Yosys stage can succeed before the timing stage fails. Every one of those paths removes the netlist again, so a `FAIL` never leaves a design for `rb pnr` or `rb power` to pick up. They are the fixed-path inputs `rb pnr` and `rb power` resolve, so a failed rerun that left the last successful run's netlist in place would have those commands place, route, and power-analyse a design that is no longer what the RTL says. A failed run therefore leaves no netlist at all, and `rb pnr` reports that you need to run `rb synth` first. Copy a netlist you want to compare against out of the artefact directory before rerunning.

When a run fails, inspect the relevant stage log first. Missing tools, plugin paths, Liberty, or LEF inputs are configuration failures; correct the path or installation and rerun the named synthesis.
