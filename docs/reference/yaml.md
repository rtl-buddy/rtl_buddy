# YAML Formats

This page is the canonical reference for all `rtl_buddy` configuration files. Use it when creating or updating configs for new designs, suites, and regressions.

## root_config.yaml

The root config lives at the project root. It defines platforms, builders, Verible, and the default regression config path.

**Required keys:**

- `rtl-buddy-filetype: project_root_config`
- `cfg-platforms`
- `cfg-rtl-builder`
- `cfg-verible`
- `cfg-rtl-reg`

**Full example:**

```yaml
rtl-buddy-filetype: project_root_config

cfg-platforms:
  - os: "osx"
    unames: ["Darwin"]
    builder: "verilator"
    verible: "verible-macos"

cfg-rtl-builder:
  - name: "verilator"
    builder: "verilator"
    builder-simv: "obj_dir/simv"
    sim-rand-seed: 31310
    sim-rand-seed-prefix: "+verilator+seed+"
    builder-opts:
      debug:
        compile-time: "--binary -sv -o simv"
        run-time: "+verilator+rand+reset+2"
      reg:
        compile-time: "--binary -sv -o simv"
        run-time: "+verilator+rand+reset+2"

cfg-verible:
  - name: "verible-macos"
    path: "tools/verible/macos/active/bin"
    extra_args:
      lint:
        - "--rules=-module-filename"

cfg-coverage:
  - name: "verilator"
    use-lcov: true

cfg-coverview:
  - name: "verilator"
    generate-tables: "line"
    config:
      # inline Coverview JSON configuration values

cfg-rtl-reg:
  reg-cfg-path: "design/regression.yaml"
```

**Runtime effects:**

- Platform is selected by matching `uname` output against `cfg-platforms[].unames`.
- `--builder` overrides the platform-selected builder for the current run.
- `--builder-mode` selects which named `builder-opts` entry to use for compile-time and run-time flags.
- `cfg-coverage` is keyed by simulator family (e.g. `verilator`). `use-lcov: true` enables `.info` export and LCOV HTML generation when `--coverage-html` is used.
- `cfg-coverview` is keyed by simulator family. `generate-tables` sets the coverage type for Coverview tables. `config` is a dict of inline Coverview JSON configuration values.
- `cfg-rtl-reg.reg-cfg-path` is the fallback regression file for `rtl-buddy regression` when no `./regression.yaml` exists in the cwd.

---

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
- `regression` changes directory into each suite directory before running.

---

## models.yaml

**Required keys:**

- `rtl-buddy-filetype: model_config`
- `models`

**Example:**

```yaml
rtl-buddy-filetype: model_config

models:
  - name: "my_design"
    filelist:
      - "-F my_design.f"
```

**Runtime effects:**

- `tests.yaml` references a model by `name` using the `model` and `model_path` fields.
- Model filelists are parsed by the filelist logic: `-F` recursion, `+incdir+`, `+libext+`, `-v`, `-y`, and plain source paths are all supported.

---

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

### cocotb testbenches

Adding a `cocotb:` block to a testbench entry switches the runner to cocotb/VPI mode (Verilator only for now). `toplevel:` is required when `cocotb:` is present; omitting it raises a fatal error at config-load time.

**Prerequisite:** `cocotb` must be installed in the active Python environment (`uv add cocotb` or `pip install cocotb`). The runner invokes `cocotb-config` at compile time; a missing binary surfaces as a `FatalRtlBuddyError` with an actionable message.

```yaml
testbenches:
  - name: "tb_my_design"
    filelist:
      - "my_design.sv"
    toplevel: my_design          # DUT top-level module name — required for cocotb
    cocotb:
      module: test_my_design     # Python module(s) containing @cocotb.test() coroutines

  - name: "tb_multi"
    filelist:
      - "my_design.sv"
    toplevel: my_design
    cocotb:
      module:                    # list form: all modules are loaded
        - test_smoke
        - test_corner_cases
```

**Pass/fail detection for cocotb testbenches:**

cocotb writes a JUnit XML results file (`cocotb_results.xml`) instead of `PASS`/`FAIL` stdout lines. `rtl_buddy` parses this file automatically after simulation; you do not need `$display("PASS …")` in cocotb tests. The `desc` field in the result reports the first three failure messages and a `(+N more)` suffix when there are more.

**Testbench field reference (cocotb-specific additions):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `toplevel` | string | Yes (cocotb only) | Top-level DUT module name passed to `COCOTB_TOPLEVEL` |
| `cocotb.module` | string or list | Yes | Python test module(s) passed to `COCOTB_TEST_MODULES` |

**Runtime effects by field:**

- `testbench`: selects entry from `testbenches`; its filelist is appended to model sources for compilation.
- `model_path`: resolved relative to the `tests.yaml` file's directory.
- `reglvl` as dict: use `default` as the fallback for builders not listed.
- `plusdefines`: converted to `+define+KEY` (no value) or `+define+KEY=VALUE`.
- `plusargs`: converted to `+KEY` (no value) or `+KEY=VALUE`.
- `sim_timeout`: applies per test run, not per iteration in `randtest`.
- `sweep.path`: Python script that expands one test entry into a list of `TestConfig` objects. See [Plugins](../concepts/plugins.md).
- `preproc.path`: Python script executed before compile; can mutate `test_cfg` and `root_cfg`. See [Plugins](../concepts/plugins.md).

## Path semantics and cwd

- `rtl_buddy.log` and the convenience symlinks (`test.log`, `test.err`, `test.randseed`) are written to the suite root (the current working directory).
- Per-test artifacts are written to `artefacts/{test_name}/` under the suite root. Single runs write `test.log`, `test.err`, `test.randseed`, `compile.log`, `run.f`, and (if enabled) `coverage.dat` there directly. Repeated runs (`randtest`) write sim outputs into numbered subdirectories: `artefacts/{test_name}/run-0001/`, etc.
- `test` and `randtest` do **not** automatically change into the suite directory. Run from the suite directory, or use `--test-config` with a full path.
- `regression` does `chdir` into each suite directory before executing.
- For portable configs in multi-suite repos, make paths in `tests.yaml` explicit and verify they resolve correctly from the intended invocation directory.

## Authoring checklist for new suites

1. Add or verify the model entry in `models.yaml`.
2. Add a `testbench` entry and verify the filelist paths resolve correctly.
3. Add at least one test entry with `model`, `model_path`, and `testbench`.
4. Set `reglvl` policy: `0` for must-run sanity tests, larger values for extended tests.
5. Add the suite path to `regression.yaml`.
6. Run a smoke pass:

   ```bash
   rtl-buddy --machine test <name> -c <suite>/tests.yaml
   rtl-buddy --machine regression -c <regression.yaml> -s 0 -l 0
   ```
