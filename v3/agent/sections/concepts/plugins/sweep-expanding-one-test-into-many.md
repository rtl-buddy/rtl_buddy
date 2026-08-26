## Sweep: expanding one test into many

The sweep hook runs before the test flow and expands a single test entry into multiple `TestConfig` objects, each with different parameters. Use it to cover a combinatorial space of plusargs, seeds, or configurations without manually listing every variant.

**`tests.yaml` entry:**

```yaml
- name: "sweep_case"
  sweep:
    path: "example_sweep.py"
  model: "my_design"
  model_path: "../src/models.yaml"
  testbench: "tb_top"
  reglvl: 2000
```

**Available variables in the script:**

| Variable | Type | Description |
|----------|------|-------------|
| `logger` | Logger | Use this for all logging so output goes through `rtl_buddy`'s log system |
| `test_cfg` | TestConfig (immutable) | The original test entry from `tests.yaml` |
| `root_cfg` | RootConfig (mutable) | The loaded root config |
| `suite_dir` | string | Absolute path to the directory containing `tests.yaml` |
| `artifact_dir` | string | Artifact root for the incoming test name under `suite_dir/artefacts/` |
| `out_test_cfgs` | list | **Assign** the expanded list of `TestConfig` objects here |
| `__file__` | string | Absolute path to the current sweep script |

Everything in `TestConfig` except `reglvl` can be mutated in the generated tests (e.g. change `name`, `plusargs`, `plusdefines`).

**Example:**

```python
# example_sweep.py
out_test_cfgs = []
for i in range(4):
    cfg = test_cfg.copy()
    cfg.name = f"{test_cfg.name}_{i}"
    cfg.plusargs["SCENARIO"] = str(i)
    out_test_cfgs.append(cfg)
```

If the sweep script raises an exception, `rtl_buddy` records that test as a setup failure and continues with the remaining tests.

See the template repo for a working example.
