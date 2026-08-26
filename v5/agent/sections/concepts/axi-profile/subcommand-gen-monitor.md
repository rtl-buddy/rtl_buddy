## Subcommand: `gen-monitor`

```bash
# Emit SV monitor at model.axi_monitor_out
rb axi-profile gen-monitor soc_top

# Custom output path
rb axi-profile gen-monitor soc_top -o /tmp/axi_perf_mon.sv

# Match the testbench's `timeprecision`
rb axi-profile gen-monitor soc_top --time-precision 1ps

# Cap per-bundle FIFO depth (drained only at $finish)
rb axi-profile gen-monitor soc_top --buffer-cap 16384
```

The runner reads the manifest from `model.axi_bundles` and invokes `axi-profiler gen-monitor <manifest> --output <path>`. The generated `.sv` file uses SystemVerilog `bind` semantics so the monitor instances live alongside the DUT without modifying its source.

You add the generated SV to the testbench's filelist once. If `axi_monitor_out:` points at a path inside the verif tree (e.g. `../verif/soc_top/gen/axi_perf_mon.sv`), that's a one-time step — subsequent `gen-monitor` runs just rewrite the file in place.

`--time-precision` must match the IEEE 1800 `timeprecision` of the wrapping testbench, otherwise the monitor's timestamp arithmetic will be off by a power of ten. `--buffer-cap` bounds memory growth on extremely long traces — the buffer is drained to disk only at `$finish`.
