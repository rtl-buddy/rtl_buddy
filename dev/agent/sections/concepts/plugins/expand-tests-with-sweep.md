## Expand tests with `sweep`

A sweep runs before the test flow and replaces one test with a list of variants. Assign the variants to `out_test_cfgs`:

```yaml
- name: sweep_case
  sweep:
    path: example_sweep.py
  model: my_design
  model_path: ../src/models.yaml
  testbench: tb_top
  reglvl: 2000
```

```python
out_test_cfgs = []
for i in range(4):
    cfg = test_cfg.copy()
    cfg.name = f"{test_cfg.name}_{i}"
    cfg.plusargs["SCENARIO"] = str(i)
    out_test_cfgs.append(cfg)
```

| Variable | Value |
|---|---|
| `test_cfg` | Original immutable `TestConfig`; copied variants may change any field except `reglvl` |
| `root_cfg` | Mutable `RootConfig` |
| `suite_dir` | Absolute directory containing `tests.yaml` |
| `artifact_dir` | Artefact root for the incoming test name |
| `out_test_cfgs` | Output list the script must assign |
| `logger` | rtl_buddy logger |
| `__file__` | Absolute hook path |

A script exception marks the source test as a setup failure; remaining tests continue.
