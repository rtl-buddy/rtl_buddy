## Synthesis regression: `synth_regression.yaml`

`synth_regression.yaml` lists the `synth.yaml` files to include in a synthesis regression:

```yaml
rtl-buddy-filetype: synth_reg_config

synth-configs:
  - "synth/sandbox/synth.yaml"
  - "synth/dma/synth.yaml"
```

Paths are resolved relative to `synth_regression.yaml`.
