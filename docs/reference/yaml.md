---
description: Canonical reference for rtl_buddy YAML configuration files, including root_config.yaml, regression.yaml, tests.yaml, models.yaml, synth.yaml, synth_regression.yaml, cdc.yaml, and cdc_regression.yaml.
---

# YAML Formats

This page is the canonical reference for all `rtl_buddy` configuration files. Use it when creating or updating configs for new designs, suites, and regressions.

## root_config.yaml

The root config lives at the project root. It defines platforms, builders, Verible, coverage, synthesis tools, synthesis libraries, and the default regression config path.

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
    path: "/opt/homebrew/bin"
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

cfg-surfer:
  - name: "surfer-default"
    path: "surfer"              # bare name → found via PATH; or relative/absolute path
    wcp-port: 0         # 0 = OS auto-assigns a free port
    editor-cmd: "vim +%l %f"   # %f = file path, %l = line number
    editor-terminal: "tmux"    # tmux | iterm2 | terminal | "" (empty = run cmd directly)
    editor-sock: "~/.local/share/rtl-buddy/wave-nvim.sock"  # optional: nvim remote reuse
    ctrl-sock: "~/.local/share/rtl-buddy/wave-ctrl.sock"    # optional: nvim → Surfer

cfg-synth-tools:
  - name: "yosys"
    tool: "yosys"
    opts:
      synth-args: ""
      abc-args: ""
  - name: "openroad"
    tool: "openroad"
    opts:
      strategy: "AREA"   # AREA | TIMING | TIMING_ANNEAL | TIMING_GENETIC

cfg-synth-libs:
  - name: "sky130hd_tt"
    path: "pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"
    lef-paths:           # required for OpenROAD backend
      - "pdk/sky130hd/lef/sky130_fd_sc_hd_merged.lef"

cfg-cdc-tools:
  - name: "rtl-buddy-cdc"
    tool: "rtl-buddy-cdc"
    opts:
      sync-depth: 2          # forwarded as `--sync-depth N` (CDC-002 required depth)
      extra-args: ""         # appended verbatim to every invocation

cfg-rtl-reg:
  reg-cfg-path: "design/regression.yaml"
```

**Runtime effects:**

- Platform is selected by matching `uname` output against `cfg-platforms[].unames`.
- `--builder` overrides the platform-selected builder for the current run.
- `--builder-mode` selects which named `builder-opts` entry to use for compile-time and run-time flags.
- `cfg-coverage` is keyed by simulator family (e.g. `verilator`). `use-lcov: true` enables `.info` export and LCOV HTML generation when `--coverage-html` is used.
- `cfg-coverview` is keyed by simulator family. `generate-tables` sets the coverage type for Coverview tables. `config` is a dict of inline Coverview JSON configuration values.
- `cfg-surfer` configures the Surfer waveform viewer used by `rb wave`. `path` is a bare executable name (resolved via PATH) or a relative/absolute path to the binary. `editor-cmd` supports `%f` (file path) and `%l` (line number) placeholders. `editor-terminal` controls how the editor is launched: `tmux` opens a new tmux window, `iterm2` and `terminal` use AppleScript, empty string runs the command directly (suitable for GUI editors like VS Code). `editor-sock` is an optional Unix socket path that enables nvim remote reuse: rtl-buddy launches nvim with `--listen <sock>` on first use and reconnects for subsequent events. `ctrl-sock` is an optional Unix socket for the wave control server, which lets nvim send signals to Surfer — press `<Space>wa` (or your `<leader>wa`) on a signal name to add it to the waveform view. Install the bundled nvim plugin first with `rb wave-install-nvim`.
- `cfg-synth-tools` defines synthesis tool entries selected by `synth.yaml` `tool` fields. `tool` is the executable name on `PATH`. For the Yosys backend, `opts.synth-args` are appended to the `synth` command and `opts.abc-args` are used by the unmapped ABC step. For the OpenROAD backend, `opts.strategy` controls optional resynthesis (`AREA` = none, `TIMING`/`TIMING_ANNEAL` = `resynth_annealing`, `TIMING_GENETIC` = `resynth_genetic`).
- `cfg-synth-libs` defines named Liberty files for technology-mapped synthesis. `path` is resolved relative to `root_config.yaml`. The optional `lef-paths` list specifies LEF files required by the OpenROAD backend for technology loading; ignored by the Yosys backend.
- `cfg-cdc-tools` defines CDC tool entries selected by `cdc.yaml` `tool` fields. `tool` is the executable name on `PATH` (or an absolute path). `opts.sync-depth` is forwarded as `--sync-depth N` and controls CDC-002's required synchronizer depth. `opts.extra-args` is appended verbatim to every analyzer invocation.
- `cfg-rtl-reg.reg-cfg-path` is the fallback regression file for `rtl-buddy regression` when no `./regression.yaml` exists in the cwd.
- `cfg-verible[].path` is the directory containing Verible executables. Absolute paths are used as-is; relative paths are resolved from the directory containing `root_config.yaml`.

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
    desc: "Optional human-readable description"
    filelist:
      - "-F my_design.f"
    spec: "../../spec/my_design/specs.yaml"
```

**Optional fields:**

| Field | Type | Description |
|-------|------|-------------|
| `desc` | string | Human-readable model description |
| `spec` | string | Path to the block's `specs.yaml`, relative to this `models.yaml` file. Used by `rb spec check-design` to link the design model to its specification. |

**Runtime effects:**

- `tests.yaml` references a model by `name` using the `model` and `model_path` fields.
- Model filelists are parsed by the filelist logic: `-F` recursion, `+incdir+`, `+libext+`, `-v`, `-y`, and plain source paths are all supported.
- `spec` is not used at simulation time; it is only consumed by the `rb spec` traceability commands.

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
| `covers` | list of strings | IDs of spec coverage items this test addresses (e.g. `["BLOCK-COV-01"]`). Used by `rb spec check-coverage`; has no effect at simulation time. |

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
- `preproc.path`: Python script executed before compile; can mutate `test_cfg` and `root_cfg`, and receives `suite_dir` plus `artifact_dir` in its execution namespace. See [Plugins](../concepts/plugins.md).

## Path semantics and cwd

- `rtl_buddy.log` and the convenience symlinks (`test.log`, `test.err`, `test.randseed`) are written to the suite root (the current working directory).
- Per-test artifacts are written to `artefacts/{test_name}/` under the suite root. Single runs write `test.log`, `test.err`, `test.randseed`, `compile.log`, `run.f`, and (if enabled) `coverage.dat` there directly. Repeated runs (`randtest`) write sim outputs into numbered subdirectories: `artefacts/{test_name}/run-0001/`, etc.
- `test` and `randtest` do **not** automatically change into the suite directory. Run from the suite directory, or use `--test-config` with a full path.
- `regression` does `chdir` into each suite directory before executing.
- Preproc plusargs are passed to the simulator verbatim. Resolve suite-local input paths explicitly against `suite_dir`; keep output filenames artifact-relative when they should land under `artefacts/{test_name}/`.
- For portable configs in multi-suite repos, make paths in `tests.yaml` explicit and verify they resolve correctly from the intended invocation directory.

---

## synth.yaml

**Required keys:**

- `rtl-buddy-filetype: synth_config`
- `syntheses`

**Example:**

```yaml
rtl-buddy-filetype: synth_config

syntheses:
  - name: "smoke_synth"
    desc: "Synthesize my_design with the default Yosys flow"
    model: "my_design"
    model_path: "../src/models.yaml"
    tool: "yosys"
    reglvl: 0

  - name: "sky130_synth"
    desc: "Technology-mapped synthesis for SKY130 (Yosys)"
    model: "my_design"
    model_path: "../src/models.yaml"
    tool: "yosys"
    constraints: "constraints.sdc"
    libraries:
      - "sky130hd_tt"
    params:
      WIDTH: 32
    defines:
      TARGET_SYNTH: 1
    reglvl:
      default: 0
      dc: 1000
    tool_overrides:
      yosys:
        synth_args: "-flatten"

  - name: "sky130_openroad"
    desc: "Technology-mapped synthesis with OpenROAD timing analysis"
    model: "my_design"
    model_path: "../src/models.yaml"
    tool: "openroad"
    constraints: "constraints.sdc"
    libraries:
      - "sky130hd_tt"
    reglvl: 0
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Synthesis identifier; used on the CLI and in `artefacts/{name}/` |
| `desc` | string | Human-readable synthesis description |
| `model` | string | Model name from `models.yaml`; also used as the Yosys top module |
| `model_path` | string | Path to `models.yaml`, resolved relative to the `synth.yaml` file |
| `tool` | string | Synthesis tool name from `root_config.yaml` `cfg-synth-tools` |
| `constraints` | string | Optional SDC file path, resolved relative to the `synth.yaml` file |
| `params` | dict | Optional top-level parameter overrides passed through Yosys `chparam -set` |
| `defines` | dict | Optional Verilog defines passed to `read_verilog` as `-D KEY=VALUE` |
| `libraries` | list of strings | Optional Liberty library names from `cfg-synth-libs`; enables technology mapping |
| `reglvl` | int or dict | Regression level; int for all tools, dict for per-tool with `default` |
| `tool_overrides` | dict | Optional per-tool overrides for `synth_args`, `abc_args`, or `strategy`, keyed by synthesis tool name |

**Runtime effects:**

- `rtl-buddy synth` loads `synth.yaml`, resolves sources via `models.yaml`, and dispatches to the backend selected by `tool`.
- **Yosys backend** (`tool: "yosys"`): writes `synth.f` and `synth.ys`, runs Yosys, captures output in `synth.log`. Without `libraries`, emits RTLIL; with `libraries`, runs `dfflibmap` + `abc -liberty` and emits `synth_netlist.v`. Reports Gates, Area (lib-mapped only), and WNS (lib-mapped with SDC). Passes when exit code is 0 and `synth.log` has no `ERROR:` lines.
- **OpenROAD backend** (`tool: "openroad"`): requires `libraries` with `lef-paths` configured in `cfg-synth-libs`. Stage 1 runs Yosys to produce `synth_netlist.v` (logged to `synth_yosys.log`). Stage 2 runs OpenROAD with `synth.tcl` which calls `read_lef`, `read_liberty`, `read_verilog`, `link_design`, `read_sdc` (native multi-clock), and reports area/timing; output in `synth.log`. Reports Gates, Area, WNS (from `report_checks -path_delay max`), and TNS (from `report_tns`). Passes when both stages exit with code 0 and neither log contains errors.
- If `constraints` contains `create_clock` entries, the Yosys backend uses the minimum period as ABC's `-D` constraint (multi-clock workaround). The OpenROAD backend passes the full SDC to `read_sdc` without modification.

---

## synth_regression.yaml

**Required keys:**

- `rtl-buddy-filetype: synth_reg_config`
- `synth-configs`

**Example:**

```yaml
rtl-buddy-filetype: synth_reg_config

synth-configs:
  - "design/example_block_a/synth/synth.yaml"
  - "design/example_block_b/synth/synth.yaml"
```

**Runtime effects:**

- `rtl-buddy synth-regression` iterates each listed `synth.yaml` file and filters syntheses by `--reg-level`.
- Paths in `synth-configs` are resolved relative to the `synth_regression.yaml` file.
- `synth-regression` changes directory into each synthesis suite directory before executing its entries.

---

## cdc.yaml

**Required keys:**

- `rtl-buddy-filetype: cdc_config`
- `analyses`

**Example:**

```yaml
rtl-buddy-filetype: cdc_config

analyses:
  - name: "ip_cdc_handshake_lint"
    desc: "CDC lint of the request/ack handshake IP"
    model: "ip_cdc_handshake"
    model_path: "../../design/common/models.yaml"
    tool: "rtl-buddy-cdc"
    constraints: "ip_cdc_handshake.sdc"
    waivers: "ip_cdc_handshake.waivers"   # optional
    reglvl: 0

  - name: "alu_accel_lint"
    desc: "CDC lint of the ALU accelerator"
    model: "alu_accel_top"
    model_path: "../../design/alu_accel/models.yaml"
    tool: "rtl-buddy-cdc"
    constraints: "alu_accel_top.sdc"
    reglvl:
      default: 0
      rtl-buddy-cdc: 100
    tool_overrides:
      rtl-buddy-cdc:
        sync_depth: 3
        extra_args: "--strict"
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Analysis identifier; used on the CLI and in `artefacts/{name}/` |
| `desc` | string | Human-readable analysis description |
| `model` | string | Model name from `models.yaml`; also used as the top module for elaboration |
| `model_path` | string | Path to `models.yaml`, resolved relative to the `cdc.yaml` file |
| `tool` | string | CDC tool name from `root_config.yaml` `cfg-cdc-tools` |
| `constraints` | string | SDC file path, resolved relative to the `cdc.yaml` file |
| `waivers` | string | Optional waiver file path, resolved relative to the `cdc.yaml` file |
| `reglvl` | int or dict | Regression level; int for all tools, dict for per-tool with `default` |
| `tool_overrides` | dict | Optional per-tool overrides for `sync_depth` or `extra_args`, keyed by CDC tool name |

**Runtime effects:**

- `rtl-buddy cdc` loads `cdc.yaml`, resolves sources via `models.yaml`, and dispatches to the backend selected by `tool`.
- The bundled `rtl-buddy-cdc` backend invokes the standalone `rtl-buddy-cdc lint` CLI as a subprocess. The analysis receives the model's resolved filelist, the SDC, an optional waivers file, and the merged tool opts (root `cfg-cdc-tools` baseline plus any matching `tool_overrides.<tool>`).
- Each analysis writes a text report and a machine-readable JSON report under `artefacts/{name}/`; the JSON summary is parsed to populate the pass/fail/skip result for the CLI table.
- `rtl-buddy cdc <name> --list` lists configured analyses without running them.

---

## cdc_regression.yaml

**Required keys:**

- `rtl-buddy-filetype: cdc_reg_config`
- `cdc-configs`

**Example:**

```yaml
rtl-buddy-filetype: cdc_reg_config

cdc-configs:
  - "design/example_block_a/lint/cdc.yaml"
  - "design/example_block_b/lint/cdc.yaml"
```

**Runtime effects:**

- `rtl-buddy cdc-regression` iterates each listed `cdc.yaml` file and filters analyses by `--reg-level`.
- Paths in `cdc-configs` are resolved relative to the `cdc_regression.yaml` file.
- `cdc-regression` changes directory into each CDC suite directory before executing its entries.

---

## specs.yaml

`specs.yaml` lives in `spec/<block>/` and defines the functional specification for one or more design blocks. It is consumed by the `rb spec` traceability commands and has no effect on simulation.

**Required keys:**

- `rtl-buddy-filetype: spec_config`
- `blocks`

**Example:**

```yaml
rtl-buddy-filetype: spec_config

blocks:
  - name: "my_design"
    desc: "Brief description of the block"
    docs:
      - "README.md"
      - "behavior.md"
    coverage-items:
      - id: "MY-COV-01"
        desc: "Normal operation path"
      - id: "MY-COV-02"
        desc: "Error handling and recovery"
```

**Block field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Block identifier; matched against `ModelConfig.name` when resolving `spec:` links in `models.yaml`. For single-block files the name is matched unconditionally. |
| `desc` | string | Human-readable block description |
| `docs` | list of strings | Paths to markdown spec documents, relative to this `specs.yaml` file |
| `coverage-items` | list | Functional coverage items for this block |

**Coverage item fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique coverage item identifier, referenced by `covers` in `tests.yaml` |
| `desc` | string | Human-readable description of what must be tested |

See [Spec Traceability](../concepts/spec-traceability.md) for the end-to-end workflow.

---

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
