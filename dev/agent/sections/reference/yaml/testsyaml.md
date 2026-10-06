## tests.yaml

Required top-level keys are `rtl-buddy-filetype: test_config`, `testbenches`, and `tests`. Optional top-level `builder` selects the suite default, and optional top-level `compile` sizes this suite's dispatched build jobs.

```yaml
rtl-buddy-filetype: test_config

compile:
  mem: 48G
  parallel: 1

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
| `compile` | Optional | This suite's whole-job dispatch compile reservation: `cpus`, `mem`, quoted `time`, `parallel`, `split-verilate`, a `verilate` sub-block, and a [`modes`](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#per-mode-reservations) sub-block. See below |
| `preproc-sets-plusdefines` | Default true | Suite default for the test field of the same name |

The suite `compile` block layers field by field over `cfg-dispatch.compile`, which layers over `cfg-dispatch.resources`. A testbench's own `compile` overrides it per build, and omitted fields inherit. It sizes the suite's build jobs and the compile half of a simulation job that compiles for itself.

The suite value is the floor for both the verilate job and the build job of a split suite; `split-verilate: false` runs one build job instead. It is not part of the compile fingerprint, so it never invalidates a shared build stamp.

Testbench fields:

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Testbench identifier |
| `filelist` | Required | Sources appended to the model filelist |
| `resources` | Optional | Dispatch `cpus`, `mem`, and quoted `time`, plus a [`modes`](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#per-mode-reservations) sub-block. Inherited by tests |
| `compile` | Optional | This testbench's per-build dispatch compile reservation. See below |
| `toplevel` | Required for cocotb and SystemC, optional otherwise | Module the compile elaborates from. Not defaulted to `name`. See [Elaboration top](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#elaboration-top) |
| `cocotb.module` | Required for cocotb | Python module name or list, passed as `COCOTB_TEST_MODULES` |

A testbench `compile` holds `cpus`, `mem`, quoted `time`, a `verilate` sub-block of the same three fields, and a [`modes`](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#per-mode-reservations) sub-block.

- It layers field by field over the suite's `compile`, then `cfg-dispatch.compile`. Every field must be greater than zero.
- `parallel` and `split-verilate` are rejected here and inside any `modes` block, because both are job-wide.
- The suite's build job aggregates these blocks over the builds its plan runs (largest `cpus`, summed `mem` across overlapping builds, `time` as the makespan of a `parallel`-worker queue) and floors the result at the suite-level value. Each phase of a split build aggregates its own fields the same way. A simulation job that compiles for itself uses its own testbench's value.

See [Set compile resources per suite and testbench](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#set-compile-resources-per-suite-and-testbench) for the aggregation rules.

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
| `sim-rand-seed` | Optional | Fixed runtime seed from 1 through 2147483647. Overrides `--master-seed`, `--rnd-new`, and `--rnd-last`; use it for timing or command-cycle stimulus that must not change |
| `sim-rand-seed-plusarg` | Optional | Plusarg name that receives the fixed, master-derived, or builder-default seed before `preproc` runs. A hook reads it with `test_cfg.get_plusarg(NAME)` or `test_cfg.get_resolved_seed()`. With this field set, `--rnd-new` and `--rnd-last` require a fixed `sim-rand-seed`, because their seed is not available before preprocessing |
| `uvm.max_warns` / `uvm.max_errors` | Optional | Thresholds whose excess fails the test |
| `sweep.path` | Optional | Expansion hook path |
| `preproc.path` | Optional | Precompile hook path |
| `preproc-sets-plusdefines` | Default true; boolean | `false` declares that the `preproc` hook does not change the compile key (plusdefines, builder, model, assertions). The dispatch head then counts the test's build once with identical tests in the build job's reservation, instead of once per test. Overrides the suite value. The build job compares that key before and after the hook; a hook that changes any part of it anyway logs `build_job.preproc_changed_compile_key`, naming the changed fields |
| `postproc.path` | Accepted, not executed | Custom postprocessing is unavailable |
| `covers` | Optional list | Specification coverage IDs; no simulation effect |
| `resources` | Optional | Per-test dispatch reservation layered over testbench and root defaults; quote `time`. A [`modes`](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#per-mode-reservations) sub-block is the most specific such layer |
| `assertions` | Default false | Enables Verilator `--assert` and user coverage. Other builders warn and ignore it |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

<a id="selecting-the-simulator-builder"></a>

Builder precedence is CLI `--builder`, test `builder`, suite `builder`, then the active platform default. A `reglvl` map resolves against the effective builder.

Coverage processing uses the platform-selected builder unless `--builder` is supplied. If a suite or test overrides the builder, pass `--builder` on coverage runs so simulation and coverage select the same family.

### Elaboration top

<a id="pinning-the-elaboration-top"></a>

A testbench `toplevel:` roots the compile at that module. It is passed as Verilator `--top-module`, VCS `-top`, or Icarus `-s`, and to cocotb as `COCOTB_TOPLEVEL`. Other simulator families get no top flag. It is not defaulted to the testbench `name`, which is a config label rather than a module.

- Without `toplevel:`, the simulator elects a top from filelist order. Verilator takes the first ordinary (non-`-v`) entry, so recomposing a model filelist renames the model and every emitted C++ file, and an ordinary input carrying a module nothing instantiates fails the build with `MULTITOP`.
- With `toplevel:`, a testbench missing from the composed filelist fails at compile.
- For a plain SystemVerilog testbench, `toplevel:` names the testbench, not the DUT it instantiates. A `toplevel:` that points at the DUT compiles and runs but reports `NA`; see [Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/).
- A top pinned in the builder's `compile-time` opts wins, in any spelling the family accepts. Verilator takes `--top-module`, `-top-module`, `--top`, and `-top`, and Icarus accepts the module glued to the flag (`-stb`). A disagreement logs `compile.toplevel_conflict` once per run, naming both tops. SystemC and cocotb follow the same rule.
- The flag is part of the compile fingerprint, so two testbenches over one model with different `toplevel:` values do not share a build.

### cocotb, hooks, and command root

Cocotb supports Verilator, Icarus, and VCS. `cocotb` must be installed and `cocotb-config` available. An unsupported family or a missing `toplevel` is fatal. rtl_buddy reads `cocotb_results.xml`, so cocotb tests do not need PASS/FAIL console markers.

Hooks receive the paths and variables documented in [Test plugins](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/plugins/). Generated outputs, logs, and artefacts use the directory containing `tests.yaml` as the command root. The invocation cwd does not change YAML path meaning.
