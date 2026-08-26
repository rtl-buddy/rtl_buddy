## regression.yaml

Required keys are `rtl-buddy-filetype: reg_config` and `test-configs`:

```yaml
rtl-buddy-filetype: reg_config
test-configs:
  - design/example_block_a/verif/tests.yaml
  - design/example_block_b/verif/tests.yaml
```

Paths resolve from `regression.yaml`. Each suite keeps its own command root and artefact tree. `rb regression` filters tests with `--start-level` and `--reg-level`.
