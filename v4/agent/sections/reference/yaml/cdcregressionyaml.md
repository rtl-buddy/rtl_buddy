## cdc_regression.yaml

**Required keys:**

- `rtl-buddy-filetype: cdc_reg_config`
- `cdc-configs`

**Example:**

```yaml
rtl-buddy-filetype: cdc_reg_config

cdc-configs:
  - "design/example_block_a/lint/cdc.yaml"
  - "design/example_block_b/lint/cdc.yaml"
```

**Runtime effects:**

- `rtl-buddy cdc-regression` iterates each listed `cdc.yaml` file and filters analyses by `--reg-level`.
- Paths in `cdc-configs` are resolved relative to the `cdc_regression.yaml` file.
- `cdc-regression` changes directory into each CDC suite directory before executing its entries.

---
