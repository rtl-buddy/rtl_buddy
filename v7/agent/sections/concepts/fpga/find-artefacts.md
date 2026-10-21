## Find artefacts

Each run writes `<fpga.yaml directory>/artefacts/<run>/`.

- **Vivado**: `fpga.f`, `flow.tcl`, `vivado.log`, utilization, timing, power, DRC and methodology reports, and optionally `<top>.bit`.
- **openXC7**: `fpga.f`, `synth.ys`, `yosys.log`, `<top>.json`, `nextpnr.log`, `<top>.fasm`, and optionally prjxray stage logs and `<top>.bit`.

Both backends delete their outputs before each run: reports, netlist, FASM and frames handed between stages, and the bitstream. A run that fails partway therefore leaves absent what it never wrote, not the previous run's copy. Logs are truncated by the stage that writes them.

A run without `--bitstream` removes any previously built `<top>.bit`, so a stale deployable bitstream never sits beside a run that reports none. Rerun with `--bitstream` to regenerate it, or copy the file out first.

A run whose backend tool is not installed deletes nothing. A configuration error is not a skip: an unknown `platform:` or a part the backend cannot build is reported whether or not the toolchain is present, and clears the outputs.

Do not give an FPGA run and a power run the same name within one suite. Both own `artefacts/<name>/power.rpt`, and the second to run overwrites the first. Names shared with a CDC analysis or a simulation test do not clear each other's outputs, but a directory is easier to read when one run owns it.
