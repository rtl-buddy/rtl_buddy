## fpv_regression.yaml

**Required keys:**

- `rtl-buddy-filetype: fpv_reg_config`
- `fpv-configs`

**Example:**

```yaml
rtl-buddy-filetype: fpv_reg_config

fpv-configs:
  - "design/example_block_a/fpv/fpv.yaml"
  - "design/example_block_b/fpv/fpv.yaml"
```

**Runtime effects:**

- `rtl-buddy fpv-regression` iterates each listed `fpv.yaml` file and filters verifications by `--reg-level`.
- Paths in `fpv-configs` are resolved relative to the `fpv_regression.yaml` file.
- `fpv-regression` anchors each listed FPV suite on the directory containing its `fpv.yaml` (the command root) and writes artefacts under `<that dir>/artefacts/`; it does not change the process working directory (the v5 [execution context](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/execution-context/) model).

---
