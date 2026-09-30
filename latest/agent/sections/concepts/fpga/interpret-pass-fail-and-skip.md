## Interpret pass, fail, and skip

A run passes when every backend stage exits zero, the logs contain no backend error records, required reports parse, and a requested bitstream exists. Otherwise it fails and names the failing stage or output.

A timing miss alone fails the run only with `require-timing-met: true`. Missing backend tools or data, licensing detected as unavailable during setup, and regression-level filtering return SKIP.

If a run fails:

1. Read the returned description.
2. Inspect `vivado.log` or the named openXC7 stage log.
3. Confirm executable and data paths with `rb tool-check`.
4. Fix configuration or tool errors before interpreting incomplete metrics.
