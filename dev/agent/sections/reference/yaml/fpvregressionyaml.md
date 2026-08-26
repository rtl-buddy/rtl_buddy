## fpv_regression.yaml

Required keys are `rtl-buddy-filetype: fpv_reg_config` and `fpv-configs`:

```yaml
rtl-buddy-filetype: fpv_reg_config
fpv-configs: [design/example_block/fpv/fpv.yaml]
```

Paths resolve from the manifest. Each suite retains the command root of its `fpv.yaml`; `rb fpv-regression` filters entries by `--reg-level`.
