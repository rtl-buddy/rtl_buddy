## Drive the hub from the CLI

`rb hub send` is the scripting interface to a running hub:

```bash
rb hub send state
rb hub send select demo_top.u_dma
rb hub send open-source design/dma.sv:84
rb hub send graph-focus module:dma_engine
rb hub send cov-focus file:design/dma.sv --line 84
rb hub send phys-focus module:dma_engine --metric area
rb hub send wave-add tb.dut.req tb.dut.ready
rb hub send capture --out schematic.png --format png
```

All verbs and arguments are in the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/#hub-send).

The hub keeps the latest selection and the latest graph, coverage and physical focus. A focus sent before its app opens is delivered when the app connects. A Surfer rejection, an unknown id, or an absent target peer returns a hub error and a non-zero exit.
