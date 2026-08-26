## Run synthesis

```bash
rb synth --list -c synth/block/synth.yaml
rb synth block_openroad -c synth/block/synth.yaml
rb synth -c synth/block/synth.yaml
rb synth-regression -c synth_regression.yaml
rb synth-regression -c synth_regression.yaml --reg-level 1000
```

A synthesis regression manifest lists config files relative to itself:

```yaml
rtl-buddy-filetype: synth_reg_config
synth-configs:
  - synth/block_a/synth.yaml
  - synth/block_b/synth.yaml
```
