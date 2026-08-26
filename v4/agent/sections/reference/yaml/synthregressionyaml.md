## synth_regression.yaml

**Required keys:**

- `rtl-buddy-filetype: synth_reg_config`
- `synth-configs`

**Example:**

```yaml
rtl-buddy-filetype: synth_reg_config

synth-configs:
  - "design/example_block_a/synth/synth.yaml"
  - "design/example_block_b/synth/synth.yaml"
```

**Runtime effects:**

- `rtl-buddy synth-regression` iterates each listed `synth.yaml` file and filters syntheses by `--reg-level`.
- Paths in `synth-configs` are resolved relative to the `synth_regression.yaml` file.
- `synth-regression` changes directory into each synthesis suite directory before executing its entries.

---
