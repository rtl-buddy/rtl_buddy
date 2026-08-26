## power_regression.yaml

**Required keys:**

- `rtl-buddy-filetype: power_reg_config`
- `power-configs`

**Example:**

```yaml
rtl-buddy-filetype: power_reg_config

power-configs:
  - "power/demo_block_a/power.yaml"
  - "power/demo_block_b/power.yaml"
```

**Runtime effects:**

- `rb power-regression` iterates each listed `power.yaml` and filters runs by `--reg-level`.
- Paths in `power-configs` are resolved relative to the `power_regression.yaml` file.
- Each listed suite is anchored on the directory containing its `power.yaml` (the command root); the process working directory is not changed (the v5 [execution context](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/execution-context/) model).

---
