## Run power analysis

```bash
rb power --list -c power/demo/power.yaml
rb power demo_power_saif -c power/demo/power.yaml
rb power -c power/demo/power.yaml -l 1000
rb power-regression -c power_regression.yaml -l 1000
```

A regression manifest lists power configs relative to itself:

```yaml
rtl-buddy-filetype: power_reg_config
power-configs:
  - power/block_a/power.yaml
  - power/block_b/power.yaml
```

The analysis runs on one OpenROAD thread unless the run sets `threads:` — a positive integer, or `auto` for the CPUs of the current allocation. The value is validated, clamped, emitted and recorded as `openroad_threads` exactly as for P&R; see [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#openroad-threads).
