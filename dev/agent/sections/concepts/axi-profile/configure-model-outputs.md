## Configure model outputs

Point the model at a checked-in bundle manifest and generated monitor:

```yaml
models:
  - name: soc_top
    filelist: [-F soc_top.f]
    axi_bundles: axi-bundles.yaml
    axi_monitor_out: ../verif/soc_top/gen/axi_perf_mon.sv
```

Paths are relative to `models.yaml`. `axi_bundles` is written by `discover` and read by `gen-monitor` and `run`. `axi_monitor_out` is written by `gen-monitor`; place it in the verification tree and add it to the testbench filelist once.

Both fields are optional until a command needs them. A missing required field fails before the external tool runs and points to the prerequisite step.
