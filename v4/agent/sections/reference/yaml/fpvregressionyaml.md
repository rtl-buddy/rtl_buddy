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
- `fpv-regression` changes directory into each FPV suite directory before executing its entries.

---
