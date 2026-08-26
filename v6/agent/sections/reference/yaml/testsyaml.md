## tests.yaml

Required top-level keys are `rtl-buddy-filetype: test_config`, `testbenches`, and `tests`. Optional top-level `builder` selects the suite default.

```yaml
rtl-buddy-filetype: test_config

testbenches:
  - name: tb_top
    filelist: [tb_top.sv]

tests:
  - name: smoke
    desc: Sanity test
    model: my_design
    model_path: ../src/models.yaml
    testbench: tb_top
    reglvl: 0
```

Testbench fields:

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Testbench identifier |
| `filelist` | Required | Sources appended to the model filelist |
| `resources` | Optional | Dispatch `cpus`, `mem`, and quoted `time`; inherited by tests |
| `toplevel` | Required for cocotb | DUT top passed as `COCOTB_TOPLEVEL` |
| `cocotb.module` | Required for cocotb | Python module name or list passed as `COCOTB_TEST_MODULES` |

Test fields:

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Test identifier and artefact directory name |
| `model` | Required | Model name from `models.yaml` |
| `model_path` | Required | `models.yaml` path relative to `tests.yaml` |
| `testbench` | Required | Entry in `testbenches` |
| `desc` | Required | Human-readable description |
| `reglvl` | Optional | Regression level |
| `builder` | Optional | Per-test builder override |
| `plusargs` | Optional map | `KEY: VALUE` becomes `+KEY=VALUE`; a null value becomes `+KEY` |
| `plusdefines` | Optional map | `KEY: VALUE` becomes `+define+KEY=VALUE`; a null value becomes `+define+KEY` |
| `sim_timeout` | Default 60 | Seconds per simulation run |
| `uvm.max_warns` / `uvm.max_errors` | Optional | Thresholds whose excess fails the test |
| `sweep.path` | Optional | Expansion hook path |
| `preproc.path` | Optional | Precompile hook path |
| `postproc.path` | Accepted, not executed | Custom postprocessing is unavailable |
| `covers` | Optional list | Specification coverage IDs; no simulation effect |
| `resources` | Optional | Per-test dispatch reservation layered over testbench and root defaults; quote `time` |
| `assertions` | Default false | Enables Verilator `--assert` and user coverage; other builders warn and ignore it |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

<a id="selecting-the-simulator-builder"></a>

Builder precedence is CLI `--builder`, test `builder`, suite `builder`, then the active platform default. A `reglvl` map resolves against the effective builder.

Coverage processing uses the platform-selected builder unless `--builder` is supplied. If a suite or test overrides the builder, use `--builder` for coverage runs to keep simulation and coverage family selection consistent.

Cocotb supports Verilator, Icarus, and VCS. `cocotb` must be installed and `cocotb-config` available; unsupported families or a missing `toplevel` are fatal. rtl_buddy reads `cocotb_results.xml`; cocotb tests do not need PASS/FAIL console markers.

Hooks receive the paths and variables documented in [Test plugins](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/plugins/). Generated outputs, logs, and artefacts use the directory containing `tests.yaml` as the command root; invocation cwd does not change YAML path meaning.
