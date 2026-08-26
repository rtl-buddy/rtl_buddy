## Find artefacts

Each run writes `<fpga.yaml directory>/artefacts/<run>/`.

Vivado produces `fpga.f`, `flow.tcl`, `vivado.log`, utilization/timing/power/DRC/methodology reports, and optionally `<top>.bit`.

openXC7 produces `fpga.f`, `synth.ys`, `yosys.log`, `<top>.json`, `nextpnr.log`, `<top>.fasm`, and optional prjxray stage logs plus `<top>.bit`.
