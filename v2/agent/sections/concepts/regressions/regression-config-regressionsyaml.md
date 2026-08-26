## Regression config: `regressions.yaml`

```yaml
rtl-buddy-filetype: reg_config

test-configs:
  - "design/example_block_a/verif/tests.yaml"
  - "design/example_block_b/verif/tests.yaml"
```

Each entry in `test-configs` is a path to a suite's `tests.yaml`, resolved relative to the directory where `rtl-buddy regression` is invoked (usually the repo root).

The default path to `regressions.yaml` is set in `root_config.yaml` under `cfg-rtl-reg.reg-cfg-path`. Override it per run with `--reg-config`.
