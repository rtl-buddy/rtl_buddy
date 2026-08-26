## Running CDC

```bash
# All analyses in the default ./cdc.yaml
rb cdc

# A single analysis from a specific config
rb cdc demo_cdc_full -c cdc/demo_top/cdc.yaml

# List analyses without executing
rb cdc -c cdc/demo_top/cdc.yaml --list

# Regression across multiple cdc.yaml suites, filtered by reglvl
rb cdc-regression -c cdc_regression.yaml -l 1000
```
