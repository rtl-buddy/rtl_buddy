## synth_regression.yaml

Required keys are `rtl-buddy-filetype: synth_reg_config` and `synth-configs`:

```yaml
rtl-buddy-filetype: synth_reg_config
synth-configs: [design/example_block/synth/synth.yaml]
```

Paths resolve from the manifest. Each suite retains the command root of its `synth.yaml`; `rb synth-regression` filters entries by `--reg-level`.
