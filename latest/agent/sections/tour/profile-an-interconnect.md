## Profile an interconnect

`rb axi-profile` discovers AXI bundles from the RTL, binds a monitor into the simulation, and turns the trace into per-channel utilization, back-pressure, latency percentiles, and a per-transaction table.

```bash
cd verif/demo_axi_2x2
uv run rb test basic_traffic
uv run rb axi-profile run basic_traffic
```

```text
wrote artefacts/axi/basic_traffic/axi-perf.json (4 bundles, 3210 cycles, clock=tb_axi_2x2.clk).
```

For the `in0` bundle of the 2×2 crossbar, the write-data channel reports 55% back-pressure against 4.8% utilization, which is the deliberate stall the testbench injects. `rb axi-profile notebook` opens the same data in a packaged marimo notebook, and the hub overlays it on the schematic. See [AXI Interconnect Profiling](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/axi-profile/).
