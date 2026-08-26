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
- `cdc-regression` anchors each listed CDC suite on the directory containing its `cdc.yaml` (the command root) and writes artefacts under `<that dir>/artefacts/`; it does not change the process working directory (the v5 [execution context](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/execution-context/) model).

---
