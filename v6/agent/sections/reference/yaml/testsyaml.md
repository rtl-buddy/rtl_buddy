## tests.yaml

Required top-level keys are `rtl-buddy-filetype: test_config`, `testbenches`, and `tests`. Optional top-level `builder` selects the suite default, and optional top-level `compile` sizes this suite's dispatched build job.

```yaml
rtl-buddy-filetype: test_config

compile:
  mem: 48G

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

Top-level fields:

| Field | Requirement | Meaning |
|---|---|---|
| `rtl-buddy-filetype` | Required | Must be `test_config` |
| `testbenches` | Required | Testbench definitions |
| `tests` | Required | Test definitions |
| `builder` | Optional | Suite default builder name |
| `compile` | Optional | This suite's dispatch compile reservation: `cpus`, `mem`, and quoted `time`. Layered field by field over `cfg-dispatch.compile`, which is layered over `cfg-dispatch.resources`; an omitted field inherits. Sizes the suite's build job, and the compile half of a simulation job that compiles for itself. `parallel` is not accepted here and is discarded. Not part of the compile fingerprint, so it never invalidates a shared build stamp |

Testbench fields:

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Testbench identifier |
| `filelist` | Required | Sources appended to the model filelist |
| `resources` | Optional | Dispatch `cpus`, `mem`, and quoted `time`; inherited by tests |
| `toplevel` | Required for cocotb and SystemC, optional otherwise | Module the compile elaborates from. Passed to the builder as Verilator `--top-module`, VCS `-top`, or Icarus `-s`, and to cocotb as `COCOTB_TOPLEVEL`. Not defaulted to `name` |
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

<a id="pinning-the-elaboration-top"></a>

A testbench `toplevel:` roots the compile at that module: it is passed as Verilator `--top-module`, VCS `-top`, or Icarus `-s`. Without one, the simulator elects a top from filelist order: Verilator takes the first ordinary (non-`-v`) entry, so recomposing a model filelist renames the model and every emitted C++ file, and an ordinary input carrying a module nothing instantiates fails the build with `MULTITOP`. Declaring `toplevel:` fixes both, and a testbench missing from the composed filelist then fails at compile instead of silently producing a differently-named model. It is not defaulted to the testbench `name`, which is a config label rather than a module.

For a plain SystemVerilog testbench, `toplevel:` names the **testbench**, not the DUT it instantiates. A `toplevel:` left over from when the field was only graph metadata and points at the DUT will compile and run, and report `NA`; see [Known Issues](https://rtl-buddy.github.io/rtl_buddy/v6/known-issues/).

A top pinned in the builder's `compile-time` opts wins over `toplevel:`, in any spelling the family accepts — Verilator takes `--top-module`, `-top-module`, `--top`, and `-top`, and Icarus accepts the module glued to the flag (`-stb`). A disagreement between the two logs `compile.toplevel_conflict` once per run, naming both tops. SystemC and cocotb testbenches follow the same rule: those backends emit their own top flag only when the builder pins none. Families other than Verilator, VCS, and Icarus get no top flag. The flag is part of the compile fingerprint, so two testbenches over one model with different `toplevel:` no longer share a build.

Cocotb supports Verilator, Icarus, and VCS. `cocotb` must be installed and `cocotb-config` available; unsupported families or a missing `toplevel` are fatal. rtl_buddy reads `cocotb_results.xml`; cocotb tests do not need PASS/FAIL console markers.

Hooks receive the paths and variables documented in [Test plugins](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/plugins/). Generated outputs, logs, and artefacts use the directory containing `tests.yaml` as the command root; invocation cwd does not change YAML path meaning.
