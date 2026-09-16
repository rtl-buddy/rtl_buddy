## Driving the hub from the CLI

`rb hub send` is the scripting interface to a running hub. Examples:

```bash
rb hub send state
rb hub send select demo_top.u_dma
rb hub send open-source design/dma.sv:84
rb hub send graph-focus module:dma_engine
rb hub send cov-focus file:design/dma.sv --line 84
rb hub send phys-focus module:dma_engine --metric area
rb hub send wave-add tb.dut.req tb.dut.ready
rb hub send wave-zoom 1000 2000
rb hub send capture --out schematic.png --format png
```

The command groups cover state broadcasts, waveform control and item management, schematic pan/overlay/capture, source opening, diagnostics, graph, coverage or physical focus, and coordinate resolution. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/#hub-send) for all verbs and arguments.

The hub caches the latest selection, graph focus, coverage focus, and physical focus. You can send a focus before its app opens; it is replayed when the peer registers. Each cache is one slot, latest writer wins: a late-joining pane opens on the most recent target rather than replaying a backlog. Surfer-side rejection, an unknown id, or an unavailable target peer returns a real hub error and a non-zero exit.
