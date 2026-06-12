---
description: Canonical reference for rtl_buddy YAML configuration files, including root_config.yaml, regression.yaml, tests.yaml, models.yaml, synth.yaml, synth_regression.yaml, pnr.yaml, power.yaml, power_regression.yaml, cdc.yaml, cdc_regression.yaml, fpv.yaml, fpv_regression.yaml, and mut.yaml.
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
      frontend: "verilog"              # "verilog" (default) | "slang"
      plugin-path: ""                  # required if frontend: slang — path to slang.so
  - name: "openroad"
    tool: "openroad"
    opts:
      strategy: "AREA"   # AREA | TIMING | TIMING_ANNEAL | TIMING_GENETIC
      frontend: "verilog"
      plugin-path: ""

cfg-pdks:
  - name: "sky130hd"
    site: "unithd"
    corners:
      tt: "pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"
    tech-lef:  "pdk/sky130hd/lef/sky130_fd_sc_hd.tlef"
    macro-lef: "pdk/sky130hd/lef/sky130_fd_sc_hd_merged.lef"
    cell-gds:      "pdk/sky130hd/gds/sky130_fd_sc_hd.gds"
    klayout-tech:  "pdk/sky130hd/sky130hd.lyt"
    klayout-props: "pdk/sky130hd/sky130hd.lyp"
    tie-hi: "sky130_fd_sc_hd__conb_1/HI"
    tie-lo: "sky130_fd_sc_hd__conb_1/LO"
    fill-cells: [sky130_fd_sc_hd__fill_1, sky130_fd_sc_hd__fill_2]

cfg-synth-platforms:
  - name: "sky130hd_tt"
    pdk: "sky130hd"
    corner: "tt"

cfg-pnr-platforms:
  - name: "sky130hd_tt"
    pdk: "sky130hd"
    corner: "tt"
    cts-buffer: "sky130_fd_sc_hd__clkbuf_4"
    routing-layers:
      signal: "met1-met5"
      clock:  "met3-met5"

cfg-synth-efforts:
  - name: "quick"
    yosys:
      synth-args: "-flatten"
      abc-args: "-fast"
    openroad:
      run: false               # skip OpenROAD entirely → Yosys-only fast path
  - name: "standard"
    openroad:
      run: true                # current default behaviour: STA with ideal wires
  - name: "accurate"
    openroad:
      run: true
      pre-sta-tcl: |
        initialize_floorplan -utilization 0.7 -aspect_ratio 1.0 \
          -core_space 2.0 -site unithd
        global_placement -density 0.7
        estimate_parasitics -placement

cfg-pnr-tools:
  - name: "openroad"
    tool: "openroad"            # bare name → found via PATH; or absolute path

cfg-cdc-tools:
  - name: "rtl-buddy-cdc"
    tool: "rtl-buddy-cdc"
    opts:
      sync-depth: 2          # forwarded as `--sync-depth N` (CDC-002 required depth)
      extra-args: ""         # appended verbatim to every invocation

cfg-fpv-tools:
  - name: "sby"
    tool: "sby"              # bare name → found via PATH; or absolute path
    opts:
      timeout: 600           # per-task timeout in seconds; written to sby [options]
      extra-args: ""         # appended verbatim to every sby invocation
      solver-versions:       # optional pins; map solver name → exact version
        yices: "2.6.4"       # known names: yices, z3, boolector, bitwuzla,
        z3: "4.13.0"         # btormc, abc. Hard-fails on mismatch.
      plugin-path: "tools/yosys-slang/build/slang.so"  # required when an fpv.yaml verification picks `frontend: slang`

cfg-rtl-reg:
  reg-cfg-path: "design/regression.yaml"
```

**Runtime effects:**

- Platform is selected by matching `uname` output against `cfg-platforms[].unames`.
- `--builder` overrides the platform-selected builder for the current run.
- `--builder-mode` selects which named `builder-opts` entry to use for compile-time and run-time flags.
- `cfg-coverage` is keyed by simulator family (e.g. `verilator`). `use-lcov: true` enables `.info` export and LCOV HTML generation when `--coverage-html` is used.
- `cfg-coverview` is keyed by simulator family. `generate-tables` sets the coverage type for Coverview tables. `config` is a dict of inline Coverview JSON configuration values.
- `cfg-surfer` configures the Surfer waveform viewer used by `rb wave`. `path` is a bare executable name (resolved via PATH) or a relative/absolute path to the binary. `editor-cmd` supports `%f` (file path) and `%l` (line number) placeholders. `editor-terminal` controls how the editor is launched: `tmux` opens a new tmux window, `iterm2` and `terminal` use AppleScript, empty string runs the command directly (suitable for GUI editors like VS Code). `editor-sock` is an optional Unix socket path that enables nvim remote reuse: rtl-buddy launches nvim with `--listen <sock>` on first use and reconnects for subsequent events. `ctrl-sock` is an optional Unix socket for the wave control server, which lets nvim send signals to Surfer — press `<Space>wa` (or your `<leader>wa`) on a signal name to add it to the waveform view. Install the nvim plugin first with `rb nvim-install`.
- `cfg-synth-tools` defines synthesis tool entries selected by `synth.yaml` `tool` fields. `tool` is the path to the executable, or a bare name if it is available on `PATH`. For the Yosys backend, `opts.synth-args` are appended to the `synth` command and `opts.abc-args` are used by the unmapped ABC step. For the OpenROAD backend, `opts.strategy` controls optional resynthesis (`AREA` = none, `TIMING`/`TIMING_ANNEAL` = `resynth_annealing`, `TIMING_GENETIC` = `resynth_genetic`). `opts.frontend` selects the SystemVerilog parser: `"verilog"` (default) uses Yosys's built-in `read_verilog -sv -defer` per source — fast, lazy elaboration, but a small SV subset. `"slang"` loads the [yosys-slang](https://github.com/povik/yosys-slang) plugin and calls `read_slang` instead — full SV-2017 (package imports, packed-struct typedefs, complex generates) with eager elaboration. `opts.plugin-path` points at yosys-slang's `slang.so` when `frontend: slang`; absolute paths pass through, relative paths resolve against the project root, and when it is unset the `RTL_BUDDY_SLANG_PLUGIN` environment variable is consulted instead (explicit config wins — set the env var once per machine to keep project configs portable). Both options accept per-block overrides via `synth.yaml` `tool_overrides.yosys.frontend` / `.plugin_path` (note: `tool_overrides` keys are snake_case Python attribute names, while `cfg-synth-tools.opts` uses kebab-case YAML — same field, two names, see [synthesis concept doc](../concepts/synthesis.md#systemverilog-frontend) for the convention). The override key is always `yosys` (the elaboration tool), regardless of whether the synth selects `tool: yosys` or `tool: openroad`. The OpenROAD backend runs Yosys for elaboration → write_verilog → OpenROAD reads the netlist, so its elaboration-stage opts come from the `yosys` tool config + `tool_overrides.yosys` block.
- `cfg-pdks` defines one entry per process. Each holds *all* PDK-bound assets — Liberty per corner (under `corners:`), `tech-lef` / `macro-lef`, optional `cell-gds`, KLayout `.lyt` / `.lyp` for streamout, `SITE`, and `tie-hi` / `tie-lo` / `fill-cells` for P&R. Paths are resolved relative to `root_config.yaml`. Multiple PDKs can coexist; downstream platform blocks select which one to use.
- `cfg-synth-platforms` selects a `cfg-pdks` entry + corner for synthesis. Each entry has `name` (referenced by `platform:` in `synth.yaml`), `pdk` (PDK entry name), and `corner` (optional — defaults to the first declared corner). Block-specific LEFs go on the `synth.yaml` entry (`lef-paths:`) on top of the PDK's tech/macro LEFs.
- `cfg-pnr-platforms` selects a `cfg-pdks` entry + STA corner for place-and-route. Each entry has `name` (referenced by `platform:` in `pnr.yaml`), `pdk`, optional `corner` (defaults to first corner), `cts-buffer` (clock-tree buffer cell), and `routing-layers` with `signal` / `clock` layer ranges.
- `cfg-synth-efforts` defines named synthesis effort levels referenced by `synth.yaml` `effort` fields or the `--effort` CLI flag. Each entry has optional `yosys.synth-args` / `yosys.abc-args` (merged into the Yosys stage) and an `openroad` block. When `openroad.run: false`, the runner falls back to the Yosys-only backend even if `tool: openroad` was selected — useful for a fast quick-look path that needs no LEF/STA. `openroad.pre-sta-tcl` is a raw Tcl snippet injected into `synth.tcl` between `read_sdc` and `report_checks`; use it to insert floorplan/placement/parasitic-estimation steps before timing analysis. When no `cfg-synth-efforts` entries are configured or no effort is selected, a built-in `standard` effort with all defaults is used. Precedence for the same knob: per-synthesis `tool_overrides` > `cfg-synth-efforts` > `cfg-synth-tools`.
- `cfg-pnr-tools` defines P&R tool entries selected by `pnr.yaml` `tool` fields. `tool` is the path to the executable, or a bare name if it is available on `PATH`. When `pnr.yaml` `tool` does not match a `cfg-pnr-tools` entry, the value is used as the executable name directly (bare-name on `PATH` semantics).
- `cfg-power-tools` defines power-analysis tool entries selected by `power.yaml` `tool` fields. Each entry has `name` (referenced by `tool:` in `power.yaml`) and `tool` (path to the executable, or a bare name if it is available on `PATH`). When `power.yaml` `tool` does not match a `cfg-power-tools` entry, the value is used as the executable name directly (bare-name on `PATH` semantics).
- `cfg-cdc-tools` defines CDC tool entries selected by `cdc.yaml` `tool` fields. `tool` is the path to the executable, or a bare name if it is available on `PATH`. `opts.sync-depth` is forwarded as `--sync-depth N` and controls CDC-002's required synchronizer depth. `opts.extra-args` is appended verbatim to every analyzer invocation.
- `cfg-fpv-tools` defines FPV tool entries selected by `fpv.yaml` `tool` fields. `tool` is the path to the executable, or a bare name if it is available on `PATH`. `opts.timeout` is written to the generated `.sby` `[options]` block as a per-task timeout in seconds. `opts.extra-args` is appended verbatim to every sby invocation. `opts.solver-versions` is an optional map of solver short name → exact version string (e.g. `yices: "2.6.4"`); known solvers are `yices`, `z3`, `boolector`, `bitwuzla`, `btormc`, `abc`. Each pinned solver is probed before every run and the run hard-fails with a single multi-line summary if any version does not match — protects CI reproducibility against drift in locally-installed solvers. `opts.plugin-path` is the path to the yosys-slang shared library; required when any `fpv.yaml` verification picks `frontend: slang`, ignored for the default verilog frontend. Absolute paths pass through; relative paths resolve against the project root (the directory containing `root_config.yaml`).
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
- `regression` anchors each suite on the directory containing that suite's `tests.yaml` (the command root) and writes its artefacts under `<that dir>/artefacts/`; it does not change the process working directory (the v5 [execution context](../concepts/execution-context.md) model).

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
    cdc:   "../../cdc/my_design/cdc.yaml"
    synth: "../../synth/my_design/synth.yaml#fast"
    tests: "../../verif/my_design/tests.yaml"
```

**Optional fields:**

| Field | Type | Description |
|-------|------|-------------|
| `desc` | string | Human-readable model description |
| `spec` | string | Path to the block's `specs.yaml`, relative to this `models.yaml` file. Used by `rb spec check-design` to link the design model to its specification. |
| `cdc` | string | Path to the `cdc.yaml` that owns this model's CDC analysis, relative to this `models.yaml`. Optional `#analysis_name` fragment picks one analysis from a multi-analysis file (e.g. `cdc.yaml#full_design`). Read by `rb hub` to enable the clock-domain overlay; absent → overlay unavailable. |
| `synth` | string | Path to the `synth.yaml` that owns this model's synthesis flow, relative to this `models.yaml`. Same `#synth_name` fragment semantics. Declared now for forward compatibility; no consumer reads it yet. |
| `tests` | string | Path to the `tests.yaml` that owns this model's testbench/test suite, relative to this `models.yaml`. Same `#test_name` fragment semantics. Declared now for forward compatibility; no consumer reads it yet. |

**Runtime effects:**

- `tests.yaml` references a model by `name` using the `model` and `model_path` fields.
- Model filelists are parsed by the filelist logic: `-F` recursion, `+incdir+`, `+libext+`, `-v`, `-y`, and plain source paths are all supported.
- `spec` is not used at simulation time; it is only consumed by the `rb spec` traceability commands.
- `cdc` / `synth` / `tests` are *back-pointers* — the downstream files still carry their own `model:` + `model_path:` references back to this one. The model-side entry is the source of truth for "which analysis owns this model" when there could otherwise be ambiguity (e.g. two `cdc.yaml` files reference the same model name).

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
| `assertions` | bool | When true and the builder is Verilator, compile in SVA via `--assert` (and `--coverage-user` for cover-property hits) and add an `Assertions` column to the `rb test` results table. See [Assertion-Based Verification](../concepts/abv-simulation.md). |
| `xfail` | bool | Optional, default false. Marks the test expected-to-fail, **non-strict**: a FAIL becomes `XFAIL` (a pass); an unexpected PASS becomes `XPASS` but still counts as a pass. SKIP/NA pass through. Mirrors the `fpv.yaml` field — see [Expected failures (xfail)](../concepts/expected-failures.md). |
| `xfail_strict` | bool | Optional, default false. Like `xfail` but **strict**: an unexpected PASS (`XPASS`) counts as a failure. Either flag marks the test expected-to-fail; strict wins if both are set. |

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
    platform: "sky130hd_tt"
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
    platform: "sky130hd_tt"
    effort: "accurate"     # references cfg-synth-efforts entry; overridable via --effort
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
| `platform` | string | Optional `cfg-synth-platforms` name (which references a `cfg-pdks` entry); enables technology mapping |
| `lef-paths` | list of strings | Optional block-specific LEF files (paths resolved relative to the `synth.yaml` file); appended after the PDK's tech/macro LEFs for the OpenROAD backend |
| `lib-paths` | list of strings | Optional block-specific Liberty files (paths resolved relative to the `synth.yaml` file); appended after the platform's PDK Liberty for both the Yosys and OpenROAD backends |
| `reglvl` | int or dict | Regression level; int for all tools, dict for per-tool with `default` |
| `tool_overrides` | dict | Optional per-tool overrides for `synth_args`, `abc_args`, `strategy`, `frontend`, and `plugin_path`, keyed by synthesis tool name (always `yosys` for the elaboration stage). Keys are snake_case — see the `cfg-synth-tools` note above on the kebab-vs-snake naming |
| `effort` | string | Optional effort name from `cfg-synth-efforts`; controls Yosys synth/abc args and OpenROAD `pre-sta-tcl`. Overridable per invocation with `rtl-buddy synth --effort <name>`. Omitted ⇒ built-in `standard` defaults. |
| `xfail` | bool | Optional, default false. Marks the synthesis run expected-to-fail, **non-strict**: a FAIL becomes `XFAIL` (a pass); an unexpected PASS becomes `XPASS` but still counts as a pass. See [Expected failures (xfail)](../concepts/expected-failures.md). |
| `xfail_strict` | bool | Optional, default false. Like `xfail` but **strict**: an unexpected PASS (`XPASS`) counts as a failure. Either flag marks it expected-to-fail; strict wins if both are set. |

**Runtime effects:**

- `rtl-buddy synth` loads `synth.yaml`, resolves sources via `models.yaml`, and dispatches to the backend selected by `tool`.
- **Yosys backend** (`tool: "yosys"`): writes `synth.f` and `synth.ys`, runs Yosys, captures output in `synth.log`. Without `platform`, emits RTLIL; with `platform`, runs `dfflibmap` + `abc -liberty` and emits `synth_netlist.v`. Reports Gates, Area (lib-mapped only), and WNS (lib-mapped with SDC). Passes when exit code is 0 and `synth.log` has no `ERROR:` lines.
- **OpenROAD backend** (`tool: "openroad"`): requires `platform` pointing at a `cfg-synth-platforms` entry whose PDK has `tech-lef` / `macro-lef` set. Stage 1 runs Yosys to produce `synth_netlist.v` (logged to `synth_yosys.log`). Stage 2 runs OpenROAD with `synth.tcl` which calls `read_lef`, `read_liberty`, `read_verilog`, `link_design`, `read_sdc` (native multi-clock), and reports area/timing; output in `synth.log`. Reports Gates, Area, WNS (from `report_checks -path_delay max`), and TNS (from `report_tns`). Passes when both stages exit with code 0 and neither log contains errors.
- If `constraints` contains `create_clock` entries, the Yosys backend uses the minimum period as ABC's `-D` constraint (multi-clock workaround). The OpenROAD backend passes the full SDC to `read_sdc` without modification.
- `effort` selects an entry from `root_config.yaml` `cfg-synth-efforts`. If the selected effort has `openroad.run: false`, a synthesis with `tool: openroad` falls back to the Yosys-only backend (no LEF/STA required) — this is the recommended "quick" path for iteration. The `--effort` CLI flag on `rtl-buddy synth` and `rtl-buddy synth-regression` overrides whatever is set per-synthesis.

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
- `synth-regression` anchors each listed synthesis suite on the directory containing its `synth.yaml` (the command root) and writes artefacts under `<that dir>/artefacts/`; it does not change the process working directory (the v5 [execution context](../concepts/execution-context.md) model).

---

## pnr.yaml

**Required keys:**

- `rtl-buddy-filetype: pnr_config`
- `runs`

**Example:**

```yaml
rtl-buddy-filetype: pnr_config

runs:
  - name: "demo_pnr_nangate45"
    desc: "OpenROAD P&R on Nangate45 typ corner"
    tool: "openroad"
    synth: "demo_synth_nangate45"
    synth-path: "../../synth/demo/synth.yaml"
    constraints: "../../synth/demo/constraints.sdc"
    platform: "nangate45_typ"
    floorplan:
      utilization: 0.55
      aspect: 1.0
      core-margin: 2.0
    reglvl: 1000
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | P&R run identifier; used on the CLI and in `artefacts/<name>/` |
| `desc` | string | Human-readable description |
| `tool` | string | Backend tool — `"openroad"` is the only supported value today |
| `synth` | string | Name of the upstream `rb synth` entry that produced the netlist |
| `synth-path` | string | Path to the `synth.yaml` that defines `synth`, resolved relative to `pnr.yaml` |
| `constraints` | string | Path to the SDC file (required), resolved relative to `pnr.yaml` |
| `platform` | string | `cfg-pnr-platforms` entry name |
| `lef-paths` | list of strings | Optional design-specific macro LEF files (e.g. SRAM macros), resolved relative to `pnr.yaml`; emitted as extra `read_lef` lines after the platform's tech/macro LEF |
| `lib-paths` | list of strings | Optional design-specific macro Liberty files, resolved relative to `pnr.yaml`; emitted as extra `read_liberty` lines |
| `floorplan.utilization` | float | Core utilization (0–1) |
| `floorplan.aspect` | float | Die aspect ratio |
| `floorplan.core-margin` | float | Margin between core area and die edge, in microns |
| `reglvl` | int or dict | Regression level for filtering; same semantics as `synth.yaml` reglvl (int for all tools, dict for per-tool with `default`) |
| `tool_overrides` | dict | Reserved for tool-specific overrides (none consumed today) |
| `xfail` | bool | Optional, default false. Marks the pnr run expected-to-fail, **non-strict**: a FAIL becomes `XFAIL` (a pass); an unexpected PASS becomes `XPASS` but still counts as a pass. See [Expected failures (xfail)](../concepts/expected-failures.md). |
| `xfail_strict` | bool | Optional, default false. Like `xfail` but **strict**: an unexpected PASS (`XPASS`) counts as a failure. Either flag marks it expected-to-fail; strict wins if both are set. |

**Runtime effects:**

- `rb pnr` loads `pnr.yaml`, resolves the upstream `synth-path` + `synth` to find `<synth_dir>/artefacts/<synth_name>/synth_netlist.v`, and dispatches to the OpenROAD backend.
- The backend writes `pnr.tcl` from a bundled template, invokes `openroad -no_init -exit -log artefacts/<name>/pnr.log artefacts/<name>/pnr.tcl`, and produces routed DEF + post-route netlist/SDC + timing/DRC reports under `artefacts/<name>/`.
- The selected `cfg-pnr-platforms` entry provides Liberty, tech-LEF, macro-LEF, SITE, tie cells, fill cells, CTS buffer, and routing layer ranges via its referenced `cfg-pdks` entry.
- Pass when OpenROAD exits 0 and the log has no `[ERROR ...]` lines. SKIP when the entry's `reglvl` is above `--reg-level` or `tool:` is not `openroad`.

---

## power.yaml

**Required keys:**

- `rtl-buddy-filetype: power_config`
- `runs`

**Example:**

```yaml
rtl-buddy-filetype: power_config

runs:
  - name: "demo_power_static"
    desc: "Static power on Nangate45 typ corner"
    tool: "openroad"
    mode: "static"
    synth: "demo_synth_nangate45"
    synth-path: "../../synth/demo/synth.yaml"
    constraints: "../../synth/demo/constraints.sdc"
    platform: "nangate45_typ"
    reglvl: 1000

  - name: "demo_power_postpnr"
    desc: "Post-PnR power from the routed ODB"
    tool: "openroad"
    mode: "dynamic"
    netlist-source: "pnr"
    pnr: "demo_pnr_nangate45"
    pnr-path: "../../pnr/demo/pnr.yaml"
    platform: "nangate45_typ"
    activity:
      saif: "../../verif/demo/artefacts/csr_smoke/dump.saif"
      scope: "tb_top/u_dut"
    reglvl: 1000
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Run identifier; used on the CLI and in `artefacts/<name>/` |
| `desc` | string | Human-readable description (required — no default) |
| `tool` | string | Backend tool name; default `"openroad"` (the only backend today) |
| `mode` | string | `"static"` (default) or `"dynamic"`. Static skips activity; dynamic applies an activity source |
| `netlist-source` | string | `"synth"` (default) or `"pnr"`. Selects post-synth netlist vs post-PnR routed ODB |
| `synth` | string | Upstream `rb synth` entry name — **required when** `netlist-source: synth` |
| `synth-path` | string | Path to the `synth.yaml`, relative to `power.yaml` — required when `netlist-source: synth` |
| `pnr` | string | Upstream `rb pnr` entry name — **required when** `netlist-source: pnr` |
| `pnr-path` | string | Path to the `pnr.yaml`, relative to `power.yaml` — required when `netlist-source: pnr` |
| `constraints` | string | SDC path (required for `synth` source; optional for `pnr` source — defaults to the post-CTS `<top>.routed.sdc`) |
| `platform` | string | `cfg-pnr-platforms` entry name — reused for Liberty + corner |
| `activity.saif` | string | Path to a SAIF v2 file (mutually exclusive with `vcd`) |
| `activity.vcd` | string | Path to a VCD trace (mutually exclusive with `saif`) |
| `activity.scope` | string | Hierarchical scope for OpenROAD's `-scope`. Only valid alongside `saif`/`vcd`; set without a trace it raises a config-load error |
| `activity.default-toggle-rate` | float | Synthetic global toggle rate (used in `dynamic` mode with no trace). Default `0.1` |
| `activity.default-static-prob` | float | Synthetic global duty cycle. Default `0.5` |
| `reglvl` | int or dict | Regression level for filtering; same semantics as `synth.yaml`/`pnr.yaml` reglvl |
| `tool_overrides` | dict | Reserved for tool-specific overrides; accepted but not consumed by the OpenROAD backend today (mirrors `pnr.yaml`) |
| `xfail` | bool | Optional, default false. Marks the power run expected-to-fail, **non-strict**: a FAIL becomes `XFAIL` (a pass); an unexpected PASS becomes `XPASS` but still counts as a pass. See [Expected failures (xfail)](../concepts/expected-failures.md). |
| `xfail_strict` | bool | Optional, default false. Like `xfail` but **strict**: an unexpected PASS (`XPASS`) counts as a failure. Either flag marks it expected-to-fail; strict wins if both are set. |

**Runtime effects:**

- `rb power` resolves the netlist per `netlist-source`: `synth` reads `synth_netlist.v` from the upstream `rb synth` run (`read_verilog`); `pnr` reads `<top>.routed.odb` from the upstream `rb pnr` run (`read_db`) and runs `estimate_parasitics -global_routing` for routing-derived wire-cap (no SPEF). See the [Power Analysis concept page](../concepts/power.md) for the full activity-source matrix.
- The resolved activity source (`default` / `synthetic` / `saif` / `vcd`) is decided at config load and surfaced in the results table.
- Pass when `openroad` exits 0, the log has no `[ERROR ...]` lines, and the `Total` line in `power.rpt` parses. SKIP when the entry's `reglvl` is above `--reg-level` or `tool:` is not in the backend registry.

---

## power_regression.yaml

**Required keys:**

- `rtl-buddy-filetype: power_reg_config`
- `power-configs`

**Example:**

```yaml
rtl-buddy-filetype: power_reg_config

power-configs:
  - "power/demo_block_a/power.yaml"
  - "power/demo_block_b/power.yaml"
```

**Runtime effects:**

- `rb power-regression` iterates each listed `power.yaml` and filters runs by `--reg-level`.
- Paths in `power-configs` are resolved relative to the `power_regression.yaml` file.
- Each listed suite is anchored on the directory containing its `power.yaml` (the command root); the process working directory is not changed (the v5 [execution context](../concepts/execution-context.md) model).

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
    frontend: "slang"   # opt this analysis into the slang elaboration frontend
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
| `frontend` | string | Optional elaboration frontend selector forwarded as-is via `--frontend <value>` to the analyzer subprocess. The set of accepted values is the analyzer's, not rtl_buddy's — for the bundled `rtl-buddy-cdc` backend on current main it's `"yosys"` (built-in) or `"slang"` (full SV-2017 via the optional `pyslang`-backed `[slang]` extra); see the analyzer's own docs for the authoritative list. Unknown values are rejected by the analyzer, not by rtl_buddy. Omit to use the analyzer's own default. |
| `xfail` | bool | Optional, default false. Marks the CDC analysis expected-to-fail, **non-strict**: a FAIL becomes `XFAIL` (a pass); an unexpected PASS becomes `XPASS` but still counts as a pass. Useful for a design with known/intentional CDC violations tracked in a suite. See [Expected failures (xfail)](../concepts/expected-failures.md). |
| `xfail_strict` | bool | Optional, default false. Like `xfail` but **strict**: an unexpected PASS (`XPASS`) counts as a failure. Either flag marks it expected-to-fail; strict wins if both are set. |

**Runtime effects:**

- `rtl-buddy cdc` loads `cdc.yaml`, resolves sources via `models.yaml`, and dispatches to the backend selected by `tool`.
- The bundled `rtl-buddy-cdc` backend invokes the standalone `rtl-buddy-cdc lint` CLI as a subprocess. The analysis receives the model's resolved filelist, the SDC, an optional waivers file, the merged tool opts (root `cfg-cdc-tools` baseline plus any matching `tool_overrides.<tool>`), and — when set — `--frontend <value>` from the per-analysis `frontend` field.
- `frontend` is **per-analysis** (not on `cfg-cdc-tools` opts) — different from the synth side, where the equivalent selector lives on `cfg-synth-tools.opts.frontend`. Per-analysis suits the CDC use case because slang-required and Yosys-only analyses commonly coexist in one suite, and there is no useful project-wide default.
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
- `cdc-regression` anchors each listed CDC suite on the directory containing its `cdc.yaml` (the command root) and writes artefacts under `<that dir>/artefacts/`; it does not change the process working directory (the v5 [execution context](../concepts/execution-context.md) model).

---

## fpv.yaml

**Required keys:**

- `rtl-buddy-filetype: fpv_config`
- `verifications`

**Example:**

```yaml
rtl-buddy-filetype: fpv_config

verifications:
  - name: "demo_fpv_fifo"
    desc: "Bounded proof of FIFO interface assertions"
    tool: "sby"
    model: "demo_fifo"
    model_path: "../../design/demo_fifo/models.yaml"
    top: "demo_fifo"
    constraints: "shared_clock_reset.sv"   # optional environment assumes
    properties:
      - "demo_fifo_props.sv"
    mode: "bmc"
    depth: 32
    engines:
      - "smtbmc yices"
    reglvl: 1000

  - name: "alu_accel_fpv"
    desc: "k-induction prove of ALU accelerator invariants"
    tool: "sby"
    model: "alu_accel_top"
    model_path: "../../design/alu_accel/models.yaml"
    properties:
      - "alu_accel_props.sv"
    mode: "prove"
    depth: 16
    engines:
      - "smtbmc z3"
      - "abc pdr"
    reglvl:
      default: 0
      sby: 1000
    xfail: false           # expected-fail, non-strict (XPASS still passes)
    xfail_strict: false    # expected-fail, strict (XPASS counts as a failure)
    tool_overrides:
      sby:
        timeout: 1800
        extra_args: ""
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Verification identifier; used on the CLI and in `artefacts/{name}/` |
| `desc` | string | Human-readable verification description |
| `tool` | string | FPV tool name from `root_config.yaml` `cfg-fpv-tools` |
| `model` | string | Model name from `models.yaml` |
| `model_path` | string | Path to `models.yaml`, resolved relative to the `fpv.yaml` file |
| `top` | string | Top module name passed to `prep -top`; defaults to `model` |
| `properties` | list | SystemVerilog files containing SVA properties / bound checkers, resolved relative to `fpv.yaml`. Optional when properties are in-RTL under `` `ifdef FORMAL `` guards |
| `constraints` | string | Optional path to a single `.sv` file with environment `assume property` statements (clock toggle, reset sequence, etc.). Read into the sby script *before* `properties:` so the assumes are in scope when asserts elaborate. Resolved relative to `fpv.yaml`. Analogous to `constraints:` in `pnr.yaml` — separates "environment" from "what to prove" and lets multiple verifications share one clock/reset boilerplate. |
| `mode` | string | One of `bmc`, `prove`, `cover`, `live`; defaults to `bmc` |
| `depth` | int | Cycle depth for the proof; defaults to 20 |
| `engines` | list | Sby engine specs (e.g. `smtbmc yices`, `abc pdr`); defaults to `["smtbmc yices"]` |
| `reglvl` | int or dict | Regression level; int for all tools, dict for per-tool with `default` |
| `tool_overrides` | dict | Optional per-tool overrides for `timeout` or `extra_args`, keyed by FPV tool name |
| `vacuity` | bool | Optional. When true (default for `bmc` / `prove`), run a secondary sby cover-mode pass over auto-derived covers for every `\|->` / `\|=>` antecedent in the property set. Default is false for `cover` / `live` modes. See [Vacuity covers](../concepts/fpv.md#vacuity-covers). |
| `coi` | bool | Optional. When true (default), run a yosys cone-of-influence pass after the primary proof and report the fraction of design cells reachable from at least one assertion. See [Cone-of-influence coverage](../concepts/fpv.md#cone-of-influence-coverage). |
| `frontend` | string | SystemVerilog frontend. `"verilog"` (default — yosys native, immediate + simple-concurrent SVA only) or `"slang"` (yosys-slang plugin — required for `\|->` / `\|=>` and SV `bind`). `slang` requires `cfg-fpv-tools[].opts.plugin-path` in root_config.yaml. See [Choosing a frontend](../concepts/fpv.md#choosing-a-frontend). |
| `xfail` | bool | Optional, default false. Marks the verification expected-to-fail, **non-strict**: a FAIL becomes `XFAIL` (a pass); an unexpected PASS becomes `XPASS` but still counts as a pass. See [Expected failures (xfail)](../concepts/expected-failures.md). |
| `xfail_strict` | bool | Optional, default false. Like `xfail` but **strict**: an unexpected PASS (`XPASS`) counts as a failure. A verification is expected-to-fail if either flag is set; strict wins if both are. |

**Runtime effects:**

- `rtl-buddy fpv` loads `fpv.yaml`, resolves the model's filelist via `models.yaml`, and dispatches to the backend selected by `tool`.
- The bundled `sby` backend generates a `.sby` config containing `[options]` (mode, depth, optional timeout), `[engines]`, `[script]` (Yosys read + prep), and `[files]` (resolved source paths), then invokes `sby -f -d <workdir> <config>`.
- Each verification writes the generated config, the full sby log, and the sby workdir under `artefacts/{name}/`; the workdir's `status` file is the authoritative pass/fail signal, with the process exit code as fallback.
- Counterexample VCDs (on FAIL) land at `artefacts/{name}/sby_workdir/engine_<N>/trace.vcd`.
- `rtl-buddy fpv <name> --list` lists configured verifications without running them.

---

## fpv_regression.yaml

**Required keys:**

- `rtl-buddy-filetype: fpv_reg_config`
- `fpv-configs`

**Example:**

```yaml
rtl-buddy-filetype: fpv_reg_config

fpv-configs:
  - "design/example_block_a/fpv/fpv.yaml"
  - "design/example_block_b/fpv/fpv.yaml"
```

**Runtime effects:**

- `rtl-buddy fpv-regression` iterates each listed `fpv.yaml` file and filters verifications by `--reg-level`.
- Paths in `fpv-configs` are resolved relative to the `fpv_regression.yaml` file.
- `fpv-regression` anchors each listed FPV suite on the directory containing its `fpv.yaml` (the command root) and writes artefacts under `<that dir>/artefacts/`; it does not change the process working directory (the v5 [execution context](../concepts/execution-context.md) model).

---

## mut.yaml

Unlike the other suite configs, a `mut.yaml` describes a **single mutation campaign** (one design file under test), not a list of runs. See the [Mutation Testing concept page](../concepts/mut.md) for the full workflow.

**Required keys:**

- `rtl-buddy-filetype: mut_config`
- `model`, `model_path`, `design_file`, `operators`, `verify`

**Example:**

```yaml
rtl-buddy-filetype: mut_config

model: demo_top
model_path: "../../design/demo_top/models.yaml"
design_file: "../../design/demo_top/rtl/alu.sv"

operators:
  - arith_flip
  - bit_op_flip
  - cond_negate

verify:
  fpv_config: "../../fpv/demo/fpv.yaml"
  verification: "demo_fpv_alu_safety"
  test_config: "../../verif/demo/tests.yaml"
  tests: ["alu_smoke"]
  assertions: true

budget:
  max_mutants: 100
  schedule: "sequential"
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `model` | string | Model name within the referenced `models.yaml` |
| `model_path` | string | Path to the `models.yaml`, resolved relative to `mut.yaml` |
| `design_file` | string | The single SystemVerilog file to mutate, relative to `mut.yaml`. Must live within the model directory so per-mutant isolation can copy the tree |
| `operators` | list of strings | Non-empty list of operators: `arith_flip`, `bit_op_flip`, `cond_negate`, `cond_const`, `assign_drop`, `port_binding_swap`. Empty or unknown ⇒ fatal config error |
| `verify.fpv_config` | string | Path to an `fpv.yaml`, relative to `mut.yaml` (FPV kill oracle) |
| `verify.verification` | string | Verification name in that `fpv.yaml` — **required when** `fpv_config` is set |
| `verify.test_config` | string | Path to a `tests.yaml`, relative to `mut.yaml` (simulation kill oracle) |
| `verify.tests` | list of strings | Optional subset of test names; empty (default) runs every test in the suite |
| `verify.assertions` | bool | Compile SVA in via Verilator `--assert`. Default `true` |
| `name` | string | Campaign id; used in `artefacts/mut/<name>/`. Defaults to `model` |
| `top` | string | Top module under test. Defaults to `model` |
| `budget.max_mutants` | int | Cap on mutants generated. Default `100` |
| `budget.per_file_cap` | int or null | Per-file cap (max mutants per scoped file), or `null` (default) for none |
| `budget.time_budget_minutes` | float or null | Wall-clock budget in minutes, or `null` (default) for none |
| `budget.schedule` | string | `"sequential"` (default) or `"round_robin"` |
| `scope.include` / `scope.exclude` | list of strings | Optional case-sensitive globs (shell-glob, no `**`) matched against each node's instance path and source file; selects which files to mutate. Empty = single-file default (mutate `design_file`, no rtl-buddy-view needed); non-empty ingests the `rb hier` graph and needs `rtl-buddy-view` on PATH |

**Runtime effects:**

- `verify` must configure at least one kill oracle (`fpv_config` + `verification`, and/or `test_config`); otherwise config load fails. When both are set, a mutant is killed if either oracle catches it.
- `rb mut run` writes `mut_report.json` under `<mut.yaml dir>/artefacts/mut/<campaign>/`. It exits `1` only when nothing was scorable; score thresholding is not gated.
- The mutation engine lives in the optional [`rtl-buddy-xeno`](https://github.com/rtl-buddy/rtl-buddy-xeno) package, enabled via the `[mut]` extra (`uv add "rtl_buddy[mut]"`, which pulls `rtl-buddy-xeno[verible,slang] >= 0.1.0`); `rb mut` raises a fatal error with this hint if it is missing or below the version floor.

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
