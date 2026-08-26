## Generate and compile the monitor

```bash
rb axi-profile gen-monitor soc_top
rb axi-profile gen-monitor soc_top -o /tmp/axi_perf_mon.sv
rb axi-profile gen-monitor soc_top --time-precision 1ps --buffer-cap 16384
```

The generated SystemVerilog uses `bind` so it can observe the DUT without modifying RTL. Add the generated file to the testbench filelist before simulation.

`--time-precision` must match the wrapping testbench's IEEE 1800 `timeprecision`; a mismatch scales timestamps incorrectly. `--buffer-cap` bounds each bundle's in-memory FIFO. The monitor drains its buffers only at `$finish`, so ensure the simulation exits normally.
