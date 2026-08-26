## Running FPV

```bash
# All verifications in the default ./fpv.yaml
rb fpv

# A single verification from a specific config
rb fpv demo_fpv_fifo -c fpv/demo_fifo/fpv.yaml

# List verifications without executing
rb fpv -c fpv/demo_fifo/fpv.yaml --list

# Regression across multiple fpv.yaml suites, filtered by reglvl
rb fpv-regression -c fpv_regression.yaml -l 1000
```
