## Configure model outputs

Point the model at a checked-in bundle manifest and a generated monitor file:

```yaml
models:
  - name: soc_top
    filelist: [-F soc_top.f]
    axi_bundles: axi-bundles.yaml
    axi_monitor_out: ../verif/soc_top/gen/axi_perf_mon.sv
```

Paths are relative to `models.yaml`. `discover` writes `axi_bundles`; `gen-monitor` and `run` read it. `gen-monitor` writes `axi_monitor_out`. Put that file in the verification tree and add it to the testbench filelist once.

Both fields are optional until a command needs them. A command with a missing required field fails before the external tool runs and names the step that produces it.
