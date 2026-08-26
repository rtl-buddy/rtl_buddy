## regression.yaml

**Required keys:**

- `rtl-buddy-filetype: reg_config`
- `test-configs`

**Example:**

```yaml
rtl-buddy-filetype: reg_config

test-configs:
  - "design/example_block_a/verif/tests.yaml"
  - "design/example_block_b/verif/tests.yaml"
```

**Runtime effects:**

- `rtl-buddy regression` iterates each listed suite and runs tests filtered by `--start-level`/`--reg-level`.
- `regression` anchors each suite on the directory containing that suite's `tests.yaml` (the command root) and writes its artefacts under `<that dir>/artefacts/`; it does not change the process working directory (the v5 [execution context](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/execution-context/) model).

---
