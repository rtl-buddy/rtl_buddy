## Generate and compile the monitor

```bash
rb axi-profile gen-monitor soc_top
rb axi-profile gen-monitor soc_top -o /tmp/axi_perf_mon.sv
rb axi-profile gen-monitor soc_top --time-precision 1ps --buffer-cap 16384
```

The generated SystemVerilog attaches with `bind`, so the RTL is not modified. Add the file to the testbench filelist before simulating.

- `--time-precision` must equal the wrapping testbench's IEEE 1800 `timeprecision`; a mismatch scales timestamps wrongly.
- `--buffer-cap` bounds each bundle's in-memory FIFO.
- The monitor drains its buffers only at `$finish`, so the simulation must exit normally.
