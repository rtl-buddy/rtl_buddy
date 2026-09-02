## Interpret pass, fail, and skip

A run passes when every backend stage exits zero, logs contain no backend error records, required reports parse, and a requested bitstream exists. It fails otherwise and names the failing stage or output.

Timing failure alone does not fail the run unless `require-timing-met: true`. Missing backend tools or data, licensing unavailability detected as setup, and regression-level filtering return SKIP.

If a run fails:

1. Read the returned description.
2. Inspect `vivado.log` or the named openXC7 stage log.
3. Confirm the executable and data paths with `rb tool-check`.
4. Fix configuration or tool errors before interpreting incomplete metrics.

Filelist `+incdir+` entries reach both backends: Vivado's `synth_design` gets them as `-include_dirs`, openXC7's `read_verilog` as `-I`. Each directory resolves against the filelist that declared it, so an `-F`-nested filelist can carry the include path its own sources need.
