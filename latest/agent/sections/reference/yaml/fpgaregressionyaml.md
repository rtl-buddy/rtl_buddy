## fpga_regression.yaml

Required keys are `rtl-buddy-filetype: fpga_reg_config` and `fpga-configs`:

```yaml
rtl-buddy-filetype: fpga_reg_config
fpga-configs: [fpga/counter/fpga.yaml]
```

Paths resolve from the manifest. Each suite retains the command root of its `fpga.yaml`; `rb fpga-regression` filters entries by `--reg-level`. Discovery checks `./fpga_regression.yaml` before `cfg-rtl-reg.fpga-reg-cfg-path`.
