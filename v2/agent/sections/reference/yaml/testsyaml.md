## tests.yaml

**Required keys:**

- `rtl-buddy-filetype: test_config`
- `testbenches`
- `tests`

**Example:**

```yaml
rtl-buddy-filetype: test_config

testbenches:
  - name: "tb_top"
    filelist:
      - "+incdir+../../../verif/tb"
      - "tb_top.sv"

tests:
  - name: "smoke"
    desc: "sanity test"
    reglvl: 0
    model: "my_design"
    model_path: "../src/models.yaml"
    testbench: "tb_top"
    plusargs:
      test_cycles: "50"
      lvm_verbosity: 1
    plusdefines:
      FEATURE_X: "1"
    sim_timeout: 120
    uvm:
      max_warns: 0
      max_errors: 0

  - name: "sweep_case"
    desc: "expands to many tests"
    reglvl:
      default: 2000
      vcs: 3000
    model: "my_design"
    model_path: "../src/models.yaml"
    testbench: "tb_top"
    sweep:
      path: "example_sweep.py"
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Test identifier; used in log file names |
| `desc` | string | Human-readable description |
| `reglvl` | int or dict | Regression level; int for all builders, dict for per-builder with `default` |
| `model` | string | Model name from `models.yaml` |
| `model_path` | string | Path to `models.yaml`; resolved relative to the suite directory |
| `testbench` | string | Testbench name from `testbenches` list |
| `plusargs` | dict | `KEY: VALUE` → `+KEY=VALUE` at sim runtime |
| `plusdefines` | dict | `KEY: VALUE` → `+define+KEY=VALUE` at compile time |
| `sim_timeout` | int | Timeout in seconds (default: 60) |
| `uvm.max_warns` | int | UVM warning threshold; exceeding it fails the test |
| `uvm.max_errors` | int | UVM error threshold; exceeding it fails the test |
| `sweep.path` | string | Path to sweep expansion script |
| `preproc.path` | string | Path to pre-processing script |
| `postproc.path` | string | Path to post-processing script (parsed but not yet fully active) |

**Runtime effects by field:**

- `testbench`: selects entry from `testbenches`; its filelist is appended to model sources for compilation.
- `model_path`: resolved relative to the `tests.yaml` file's directory.
- `reglvl` as dict: use `default` as the fallback for builders not listed.
- `plusdefines`: converted to `+define+KEY` (no value) or `+define+KEY=VALUE`.
- `plusargs`: converted to `+KEY` (no value) or `+KEY=VALUE`.
- `sim_timeout`: applies per test run, not per iteration in `randtest`.
- `sweep.path`: Python script that expands one test entry into a list of `TestConfig` objects. See [Plugins](https://rtl-buddy.github.io/rtl_buddy/v2/concepts/plugins/).
- `preproc.path`: Python script executed before compile; can mutate `test_cfg` and `root_cfg`. See [Plugins](https://rtl-buddy.github.io/rtl_buddy/v2/concepts/plugins/).
