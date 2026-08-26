## cdc_regression.yaml

Required keys are `rtl-buddy-filetype: cdc_reg_config` and `cdc-configs`:

```yaml
rtl-buddy-filetype: cdc_reg_config
cdc-configs: [lint/cdc/demo/cdc.yaml]
```

Paths resolve from the manifest. Each suite retains the command root of its `cdc.yaml`; `rb cdc-regression` filters analyses by `--reg-level`. Discovery checks `./cdc_regression.yaml` before `cfg-rtl-reg.cdc-reg-cfg-path`.
