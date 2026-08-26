## `models.yaml` fields

Two optional fields on each `models.yaml` entry drive `rb axi-profile`:

```yaml
models:
  - name: "soc_top"
    filelist:
      - "-F soc_top.f"
    axi_bundles: "axi-bundles.yaml"               # manifest path (input to run / gen-monitor)
    axi_monitor_out: "../verif/soc_top/gen/axi_perf_mon.sv"  # where gen-monitor writes
```

| Field | Description |
|-------|-------------|
| `axi_bundles` | Relative path from `models.yaml` to the model's checked-in `axi-bundles.yaml` manifest. Consumed by `rb axi-profile run` and `rb axi-profile gen-monitor`; produced by `rb axi-profile discover`. |
| `axi_monitor_out` | Relative path from `models.yaml` to where `rb axi-profile gen-monitor` writes the generated SV monitor. Typically points into the verif testbench source tree so the file is picked up by the tb's filelist (e.g. `../verif/soc_top/gen/axi_perf_mon.sv`). |

Both fields are optional from rtl_buddy's perspective; missing them surfaces a clear error from the subcommand that needs them, pointing at the prerequisite command.
