# Plugins

`rtl_buddy` supports three Python plugin hooks that let you extend test behavior without modifying the tool itself. All hooks are specified per-test in `tests.yaml` and are executed by the tool at the appropriate point in the test flow.

Hook scripts receive their input through named variables injected into the script's namespace. They do not use `import` or function arguments — instead they read from and write to these predefined variables.

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
| `out_test_cfgs` | list | **Assign** the expanded list of `TestConfig` objects here |

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

## Pre-processing: mutate test params before compile

The pre-processing hook runs after sweep expansion but before the compilation step. Use it to dynamically adjust plusargs, plusdefines, or other test parameters based on runtime state.

**`tests.yaml` entry:**

```yaml
- name: "basic"
  preproc:
    path: "my_preproc.py"
  model: "my_design"
  model_path: "../src/models.yaml"
  testbench: "tb_top"
```

**Available variables:**

| Variable | Type | Description |
|----------|------|-------------|
| `logger` | Logger | Use for all logging |
| `test_cfg` | TestConfig (mutable) | Modify this to change compile/sim parameters |
| `root_cfg` | RootConfig (mutable) | The loaded root config |

**Example:**

```python
# my_preproc.py
import os
test_cfg.plusargs["BUILD_ID"] = os.environ.get("CI_BUILD_ID", "local")
```

If a pre-processing script raises an exception, the affected test is marked as a setup failure and the rest of the run continues.

See the template repo for a working example.

## Post-processing

The `postproc` hook is parsed from config but the runtime flow currently relies on built-in post-processing. Custom post-processing support is planned for a future release.
