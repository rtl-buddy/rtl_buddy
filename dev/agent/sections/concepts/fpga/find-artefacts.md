## Find artefacts

Each run writes `<fpga.yaml directory>/artefacts/<run>/`.

Vivado produces `fpga.f`, `flow.tcl`, `vivado.log`, utilization/timing/power/DRC/methodology reports, and optionally `<top>.bit`.

openXC7 produces `fpga.f`, `synth.ys`, `yosys.log`, `<top>.json`, `nextpnr.log`, `<top>.fasm`, and optional prjxray stage logs plus `<top>.bit`.

Both backends delete their outputs before each run — the reports, the netlist, FASM and frames handed between stages, and the bitstream — so a run that fails partway leaves what it never wrote absent instead of the previous run's copy. The logs are exempt: each is truncated by the stage that writes it.

The bitstream goes even when the run was not asked to build one: a run without `--bitstream` removes a previously built `<top>.bit`, because the artefact directory describes the latest run and a stale deployable bitstream sitting beside a run that reports none is exactly the trap the rest of this rule closes. Rerun with `--bitstream` to regenerate it, or copy the file out first.

A run that cannot find its backend tool is the exception: it deletes nothing, because a machine without the tool never ran it and has no business removing what a machine that has it produced. A configuration error is not a skip — an unknown `platform:`, or a part the backend cannot build, is reported whether or not the toolchain is present, and clears the outputs on its way out.

Name FPGA runs distinctly from other commands' entries. An FPGA run and a power run must not share a name within one suite: both own `artefacts/<name>/power.rpt` and the second to run overwrites the first. Ownership cannot be told apart by filename, so rtl_buddy does not try — give them distinct names. Names that collide with a CDC analysis or a simulation test are safe for artifact clearing — those outputs are protected — but a shared directory is still easier to read when one run owns it.
