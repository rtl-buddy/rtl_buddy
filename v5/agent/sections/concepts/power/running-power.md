## Running power

```bash
# All runs in the default ./power.yaml
rb power

# A single run from a specific config
rb power demo_power_dynamic_saif -c power/demo/power.yaml

# Reglvl-gated runs
rb power -c power/demo/power.yaml -l 1000

# List runs without executing
rb power -c power/demo/power.yaml --list
```

### Regression

```bash
# Default: ./power_regression.yaml
rb power-regression

# Explicit config
rb power-regression -c power_regression.yaml -l 1000
```

`power_regression.yaml`:

```yaml
rtl-buddy-filetype: power_reg_config
power-configs:
  - power/demo_block_a/power.yaml
  - power/demo_block_b/power.yaml
```
