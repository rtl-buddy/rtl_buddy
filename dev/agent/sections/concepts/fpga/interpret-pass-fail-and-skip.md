## Interpret pass, fail, and skip

A run passes when every backend stage exits zero, logs contain no backend error records, required reports parse, and a requested bitstream exists. It fails otherwise and names the failing stage or output.

Timing failure alone does not fail the run unless `require-timing-met: true`. Missing backend tools or data, licensing unavailability detected as setup, and regression-level filtering return SKIP.

If a run fails:

1. Read the returned description.
2. Inspect `vivado.log` or the named openXC7 stage log.
3. Confirm the executable and data paths with `rb tool-check`.
4. Fix configuration or tool errors before interpreting incomplete metrics.

Current limitation: include-directory (`+incdir+`) entries are not propagated into the generated Vivado or openXC7 synthesis command.
