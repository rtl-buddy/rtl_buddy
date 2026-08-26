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
