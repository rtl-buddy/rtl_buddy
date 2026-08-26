## power_regression.yaml

Required keys are `rtl-buddy-filetype: power_reg_config` and `power-configs`:

```yaml
rtl-buddy-filetype: power_reg_config
power-configs: [power/demo/power.yaml]
```

Paths resolve from the manifest. Each suite retains the command root of its `power.yaml`; `rb power-regression` filters entries by `--reg-level`.
