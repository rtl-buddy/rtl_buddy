---
description: Canonical field reference for rtl_buddy project, model, test, regression, implementation, FPGA, formal, lint, CDC, XPLR, mutation, and specification YAML files.
---

# YAML Formats

Use this page for required keys, defaults, path resolution, and validation. Use the linked concept pages for procedures and interpretation.

Unless stated otherwise:

- Relative paths resolve from the YAML file that contains them. See [Execution Context](../concepts/execution-context.md).
- `reglvl` defaults to 0. It may be an integer or a per-tool/per-builder map with `default` fallback. A run is selected when its level is at most the CLI regression level.
- `xfail: true` is non-strict; `xfail_strict: true` makes an unexpected pass fail. See [Expected failures](../concepts/expected-failures.md).
- Unknown references and invalid required combinations fail during configuration loading.

## root_config.yaml

`root_config.yaml` lives at the project root and selects the platform, simulator, shared tools, physical-design data, regression manifests, and dispatch defaults.

Required top-level keys are `rtl-buddy-filetype: project_root_config`, `cfg-platforms`, `cfg-rtl-builder`, `cfg-verible`, and `cfg-rtl-reg`.

```yaml
rtl-buddy-filetype: project_root_config

cfg-platforms:
  - os: osx
    unames: [Darwin]
    builder: verilator
    verible: verible-local

cfg-rtl-builder:
  - name: verilator
    builder: verilator
    builder-simv: obj_dir/simv
    sim-rand-seed: 31310
    sim-rand-seed-prefix: +verilator+seed+
    builder-opts:
      debug:
        compile-time: --binary -sv -o simv
        run-time: +verilator+rand+reset+2

cfg-verible:
  - name: verible-local
    path: /opt/homebrew/bin

cfg-rtl-reg:
  reg-cfg-path: regression.yaml
```

### Platforms and tool paths

| Field | Requirement | Meaning |
|---|---|---|
| `cfg-platforms[].os` | Required | Platform identifier |
| `cfg-platforms[].unames` | Required | `uname` values selecting this platform |
| `cfg-platforms[].builder` | Required | Entry in `cfg-rtl-builder` |
| `cfg-platforms[].verible` | Required | Entry in `cfg-verible` |
| `cfg-platforms[].surfer` | Optional | Entry in `cfg-surfer`; otherwise `surfer-default` is used |

Every routed name is validated at load time for every platform entry. CLI selections such as `--builder` and `--surfer` override platform defaults. Per-flow `cfg-*-tools` blocks are selected by the flow YAML's `tool` and cannot be routed from `cfg-platforms`.

Executable and tool path fields accept a string or an ordered candidate list. This applies to `cfg-rtl-builder[].builder`, `cfg-verible[].path`, `cfg-surfer[].path`, `cfg-systemc.home`, and `tool` in `cfg-*-tools` entries.

- `~` and environment variables are expanded.
- Relative paths anchor to `root_config.yaml`.
- The first expanded candidate that exists wins; a bare final name is resolved through `PATH`.
- A candidate containing an unset variable is skipped. If every candidate contains an unset variable, rtl_buddy warns and retains the literal value.

Project-local environment defaults belong in [`.rtl-buddy/.env`](../concepts/root-config.md#project-local-env-defaults-rtl-buddyenv).

### Simulator builders

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Builder identifier |
| `builder` | Required | Compiler executable or candidate list |
| `builder-simv` | Required | Simulation executable path relative to the build directory; an absolute path disables cross-test shared builds |
| `sim-rand-seed` | Required | Default random seed |
| `sim-rand-seed-prefix` | Required | Simulator argument prefix for the seed |
| `builder-opts.<mode>.compile-time` | Required per used mode | Compile arguments |
| `builder-opts.<mode>.run-time` | Required per used mode | Simulation arguments |
| `simulator-family` | Optional | Backend family; otherwise inferred from the executable (`verilator`, `vcs`, or `icarus`) |
| `wave-format` | Optional | `fst-postproc` converts VCD to FST with `vcd2fst` before `rb wave`; missing `vcd2fst` falls back to VCD |
| `extra-sim-timeout` | Optional, default 0 | Non-negative seconds added to each test timeout for this builder; CLI `--extra-sim-timeout` overrides it |

`--builder-mode` selects a `builder-opts` key. A missing mode or missing compile/run stage is fatal. See [Simulator support](../concepts/simulators.md).

### Verible, coverage, and Surfer

| Block | Fields and behavior |
|---|---|
| `cfg-verible` | `name`, `path`; optional `extra_args` keyed by `lint`, `format`, `syntax`, or `preprocessor`, and `exclude` globs. Configured args precede CLI args. For the active platform, an invalid configured directory warns and falls back to `PATH` when possible |
| `cfg-coverage` | `name` is the simulator family; `use-lcov: true` enables LCOV info and HTML |
| `cfg-coverview` | `name`, `generate-tables`, and inline Coverview `config` |
| `cfg-surfer` | `name`, `path`; optional `wcp-port` (0 asks the OS), `editor-cmd` with `%f`/`%l`, `editor-terminal` (`tmux`, `iterm2`, `terminal`, or empty), `editor-sock`, and `ctrl-sock` |

See [Coverage](../concepts/coverage.md), [Waveforms](../concepts/wave.md), and the [CLI reference](cli.md) for lint commands.

### Synthesis and physical-design tools

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      synth-args: ""
      abc-args: ""
      frontend: verilog
      plugin-path: ""
      single-unit: false
      static-functions: error
      conflicting-drivers: error

cfg-pdks:
  - name: sky130hd
    site: unithd
    corners:
      tt: pdk/sky130hd/lib/tt.lib
    tech-lef: pdk/sky130hd/tech.lef
    macro-lef: pdk/sky130hd/macros.lef

cfg-synth-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    corner: tt

cfg-pnr-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    corner: tt
    cts-buffer: sky130_fd_sc_hd__clkbuf_4
    routing-layers: {signal: met1-met5, clock: met3-met5}
```

| Block | Fields and behavior |
|---|---|
| `cfg-synth-tools` | `name`, `tool`, and `opts`. Yosys options are `synth-args`, `abc-args`, `frontend`, `plugin-path`, `single-unit`, `static-functions`, and `conflicting-drivers`. OpenROAD additionally accepts `strategy` |
| `cfg-pdks` | `name`, `site`, `corners`; optional `tech-lef`, `macro-lef`, `cell-gds`, `klayout-tech`, `klayout-props`, `tie-hi`, `tie-lo`, and `fill-cells`. Paths resolve from `root_config.yaml` |
| `cfg-synth-platforms` | `name`, `pdk`, optional `corner` (first declared corner by default) |
| `cfg-pnr-platforms` | `name`, `pdk`, optional `corner`; P&R fields include `cts-buffer` and `routing-layers.signal`/`.clock` |
| `cfg-synth-efforts` | Named `yosys.synth-args`, `yosys.abc-args`, `openroad.run`, and `openroad.pre-sta-tcl` settings. Built-in default is `standard`. Precedence is per-run override, effort, tool config |
| `cfg-pnr-tools` | `name`, `tool` |
| `cfg-power-tools` | `name`, `tool` |

For synthesis, `frontend: verilog` is the default. `frontend: slang` requires `plugin-path` or `RTL_BUDDY_SLANG_PLUGIN`; relative plugin paths resolve from the project root. `single-unit` is slang-only and must be a boolean. In `synth.yaml` overrides, use snake-case keys such as `plugin_path` and `single_unit`; unknown keys warn and are ignored, while a non-mapping override or wrong `single_unit` type is fatal. The elaboration override key is `yosys` for both Yosys and OpenROAD runs. See [Synthesis](../concepts/synthesis.md#systemverilog-frontend).

`static-functions` and `conflicting-drivers` are correctness gates on the Yosys elaboration stage, which both the `yosys` and the `openroad` backend use. Omit either option to take its default:

| Option | Values | Default | Behavior |
|---|---|---|---|
| `static-functions` | `error`, `warn`, `allow` | `error` with `frontend: slang`, `warn` with `frontend: verilog` | Before Yosys starts, scans the filelist's sources and the headers they `` `include ``, for `function`/`task` declarations with no explicit `automatic` lifetime. `error` fails the run and names each `file:line: function <name>`; `warn` logs one warning per finding and records `static_function_findings` in the result envelope and machine output; `allow` skips the scan |
| `conflicting-drivers` | `error`, `allow` | `error` | After Yosys exits, fails the run when the log contains Yosys `multiple conflicting drivers` warnings, reporting the count and the log path. Warnings whose drivers are all tristate buffers and module ports are a working multi-driver bus and are not counted |

The scan resolves `` `include `` against the including file's directory and then the filelist's `+incdir+` entries, and evaluates `` `ifdef ``/`` `ifndef ``/`` `elsif ``/`` `else ``/`` `endif `` against exactly the macros Yosys is given: the run's `defines:` plus what the selected frontend predefines — `SYNTHESIS` and `YOSYS` for `read_verilog`, `SYNTHESIS` and slang's built-ins for `read_slang`. Filelist `+define+` entries are excluded because the synthesis flow does not pass them to Yosys either; a run that carries some logs one `synth.filelist_defines_ignored` warning, covering macros synthesis never sees, macros it elaborates with a different value, and bare `+define+X` entries, which cannot be compared because tools disagree about what a valueless macro expands to. The macro table follows `single-unit`: reset per source by default, shared across sources when slang reads them as one compilation unit. `` `undefineall `` follows the frontend too — slang re-applies the command-line macros, `read_verilog` does not. An unrecognized value for either option is fatal. See [Synthesis](../concepts/synthesis.md#gate-static-lifetime-subroutines).

### FPGA tools and platforms

```yaml
cfg-fpga-tools:
  - name: vivado
    tool: [/opt/Xilinx/Vivado/current/bin/vivado, vivado]
  - name: openxc7
    tool: nextpnr-xilinx

cfg-fpga-platforms:
  - name: zu7ev_board
    part: xczu7ev-ffvc1156-2-e
    board: my-zu7ev-board
    package: ffvc1156
    xdc: [constraints/board.xdc]
```

| Field | Requirement | Meaning |
|---|---|---|
| `cfg-fpga-tools[].name` | Required | Tool entry and backend name, normally `vivado` or `openxc7` |
| `cfg-fpga-tools[].tool` | Required | Executable or candidate list; relative paths anchor to `root_config.yaml` |
| `cfg-fpga-platforms[].name` | Required | Platform identifier used by `fpga.yaml` |
| `cfg-fpga-platforms[].part` | Required | Complete FPGA device part |
| `cfg-fpga-platforms[].board` | Default empty | Informational board name |
| `cfg-fpga-platforms[].package` | Default empty | Informational package name; it is not appended to `part` |
| `cfg-fpga-platforms[].xdc` | Default empty | Constraint paths relative to `root_config.yaml` |

Platform XDC files are read before a run's XDC files, so run-level constraints can override platform defaults. An unknown platform reference is fatal. See [FPGA Implementation](../concepts/fpga.md).

### Formal and other flow tools

```yaml
cfg-fpv-tools:
  - name: sby
    tool: sby
    opts:
      timeout: 600
      extra-args: ""
      plugin-path: tools/yosys-slang/build/slang.so
      solver-versions: {yices: "2.6.4", z3: "4.13.0"}
```

`cfg-fpv-tools` entries contain `name`, `tool`, and optional `opts.timeout`, `opts.extra-args`, `opts.plugin-path`, and `opts.solver-versions`. Solver pins are exact; supported names are `yices`, `z3`, `boolector`, `bitwuzla`, `btormc`, and `abc`. A mismatch is fatal. See [Formal Property Verification](../concepts/fpv.md).

Other flows use the same `name` plus executable `tool` pattern in their `cfg-*-tools` block. A flow may use its `tool` value directly as a bare executable when its backend supports that fallback.

### Tool-check version pins

```yaml
cfg-tools:
  - name: verilator
    min-version: "5.049"
  - name: verilator
    min-version: "5.050"
    platform: linux
```

`cfg-tools` overrides built-in minimum versions for `rb tool-check`. A platform-qualified entry applies only to that `cfg-platforms[].os` and takes precedence over an unqualified entry. A platform name absent from `cfg-platforms` is fatal. See [Tool dependency check](../concepts/tool-check.md).

### Regression manifest defaults

`cfg-rtl-reg.reg-cfg-path` is the fallback when `regression.yaml` is absent from the current directory. Optional flow fallbacks are `synth-reg-cfg-path`, `power-reg-cfg-path`, `fpga-reg-cfg-path`, `cdc-reg-cfg-path`, `fpv-reg-cfg-path`, and `lint-reg-cfg-path`. Relative paths resolve from `root_config.yaml`. A root-local manifest takes precedence over its fallback.

### Parallel dispatch

```yaml
cfg-dispatch:
  backend: slurm
  jobs: 4
  resources: {cpus: 2, mem: 4G, time: "01:00:00"}
  compile: {cpus: 8, mem: 16G, time: "02:00:00", parallel: 4}
  sbatch-args: [--partition=verif]
  max-jobs-per-array: 200
  max-array-size: 1001
  max-array-tasks: 1000
  poll-interval: 10
  progress-interval: 60
  max-wait: 7200
  retry:
    attempts: 2
    backoff-sec: 60
    backoff-max-sec: 600
    jitter: 0.5
    classifiers: [license-queue]
  rightsize:
    report: true
    over-threshold: 0.5
    near-limit: 0.9
    margin: 1.5
```

| Field | Default and validation |
|---|---|
| `backend` | `local`; values are `local`, `local-parallel`, `slurm`. Applies automatically to regression and randtest; `rb test` requires an explicit `--dispatch` |
| `jobs` | `min(4, CPU count)`; positive local-parallel global pool size; CLI `--jobs` wins |
| `resources.cpus` | 1; positive integer |
| `resources.mem` | Optional Slurm memory value |
| `resources.time` | `"01:00:00"`; quote it. Accepted Slurm forms are minutes, `MM:SS`, `HH:MM:SS`, and `DD-HH[:MM[:SS]]`; an integer from YAML sexagesimal parsing is fatal |
| `compile` | Inherits `resources`; reservation for the build, or folded field-by-field into workers that compile locally. A suite's own top-level `compile:` block in `tests.yaml` layers over this field by field. It is the only reservation block that takes `parallel`; the key is meaningless in a per-test or per-testbench `resources:` block, or in a suite-level `compile:`, and is discarded there |
| `compile.parallel` | 1; integer, must be at least 1. Distinct builds the suite's build job compiles concurrently. Multiplies only that job's `cpus` reservation, capped at the suite's planned test count; `mem` and `time` are submitted as written. Above 1 the job runs every config's `preproc` before any builder starts, so no hook may mutate another config's inputs. Inert where a builder compiles inside its own simulation job, since one such job is one serial build |
| `sbatch-args` | Empty list; appended verbatim and therefore overrides duplicate generated flags. Any argument here that sets the job's cpu request — `-c`/`--cpus-per-task`, or the task/node counts that raise it (`-n`/`--ntasks`, `--ntasks-per-node`, `-N`/`--nodes`) — supersedes the resolved `cpus`, so CPU right-sizing falls back to the scheduler's `ReqCPUS` for that run and its `cpus` advice names this key rather than the masked `resources.cpus` / `compile.cpus`. Within one option the last occurrence wins, as it does for sbatch; distinct options combine instead, and the advice then names them all and leaves the combining rule to sbatch rather than claiming a product. Only a lone `-c`/`--cpus-per-task` is offered the suggested value; the task/node counts are told to be decomposed. A direct `--cpus-per-task` override also disables the compile `cpus` floor, which bounds a reservation sbatch never saw; a task or node count leaves that flag in force, so the floor is kept. The `SBATCH_NTASKS`, `SBATCH_NTASKS_PER_NODE` and `SBATCH_NODES` environment variables count the same way, since the submit inherits them (command line beats environment, and the environment is never sanitized). A GPU count (`--gpus`/`-G`, `--gpus-per-node`, `--gpus-per-socket`, a gpu `--gres`, or their `SBATCH_*` forms) together with `--ntasks-per-gpu` and no `--ntasks` also counts, since sbatch derives the task count from that pair. Node-selection constraints (`--threads-per-core`, `-B`), placement maxima (`--ntasks-per-core`, `--ntasks-per-socket`, and `--ntasks-per-gpu` on its own), `--exclusive` and `SBATCH_CPUS_PER_TASK` are not overrides — the generated `--cpus-per-task` still states the request; `--cpus-per-gpu` is not either, since Slurm rejects it alongside the `--cpus-per-task` every job carries. Two exceptions to "appended last", both on the build job: its `--dependency` is emitted after these and composes the configured expression with the shared-build dedup, and its `--job-name` is emitted after these because that name is what the dedup serialises on — a `--job-name` / `-J` here therefore does not rename the build job (it still renames simulation jobs) |
| `max-jobs-per-array` | Per-array Slurm throttle, not a whole-run cap |
| `max-array-size` | Unset; the cluster's Slurm `MaxArraySize`, read from `scontrol show config` when unset. Setting it does not suppress the probe: the probe is the only source of `max-array-tasks`, which still applies. Must be at least 2. Slurm's largest array task index is one **below** it, so `1001` allows 1000 elements per array; a resource group larger than that is split across several arrays instead of being refused by sbatch. Set it where the submit host cannot run `scontrol`, or to split groups more finely |
| `max-array-tasks` | Unset; the cluster's `SchedulerParameters=max_array_tasks`, read from `scontrol show config` when unset. Must be at least 1. Unlike `max-array-size` it is an inclusive **count** of the tasks one array may hold, so `1000` allows 1000 elements. Set it where the submit host cannot run `scontrol` and the cluster caps tasks-per-array below `MaxArraySize`. Each ceiling layers independently — configured value over probed value — and the slice size is the smaller of whichever are known, so this field alone still splits a group when `MaxArraySize` cannot be resolved |
| `poll-interval` | Positive seconds between backend polls |
| `progress-interval` | 60; non-negative seconds between console updates; 0 disables console progress |
| `max-wait` | Unset; positive seconds per collection round. Expiry fails the run and cancels outstanding jobs |
| `retry.attempts` | 0; extra attempts after the first |
| `retry.backoff-sec` / `backoff-max-sec` | 60 / 600; non-negative and max must not be below initial backoff |
| `retry.jitter` | 0.5; must be in `[0, 1)` |
| `retry.classifiers` | `[license-queue]`; unknown classifiers are fatal |
| `rightsize.report` | true |
| `rightsize.over-threshold` / `near-limit` / `margin` | 0.5 / 0.9 / 1.5 |

Local-parallel ignores resource reservations and produces no right-sizing advice; `compile.parallel` still applies, being concurrency inside the build job rather than a reservation. Retry applies only to simulation jobs with license-queue evidence; Slurm additionally requires `TIMEOUT`, `NODE_FAIL`, or `PREEMPTED` and a successful build. See [Parallel dispatch](../concepts/dispatch.md).

### XPLR experiment storage

Every `cfg-xplr` field is optional:

```yaml
cfg-xplr:
  commit-mode: auto
  source-scope: ["."]
  disk-high-watermark-gb: 50
  disk-hard-cap-gb: 80
  eviction-policy: keep-frontier
  worktree-root: artefacts/xplr/worktrees
```

| Field | Default and validation |
|---|---|
| `commit-mode` | `auto`; values are `auto` and `self-managed` |
| `source-scope` | `["."]`; must be a non-empty list with no blank path |
| `disk-high-watermark-gb` | 50.0; non-negative garbage-collection threshold |
| `disk-hard-cap-gb` | 80.0; non-negative and not below the high watermark |
| `eviction-policy` | `keep-frontier`; values are `keep-frontier`, `oldest-first`, and `manual` |
| `worktree-root` | `artefacts/xplr/worktrees`; must be non-blank. Relative paths resolve from the project root |

Unknown keys and malformed values are fatal. When `root_config.yaml` or `cfg-xplr` is absent, XPLR uses these defaults. Keep `worktree-root` under a gitignored path so experiment worktrees do not dirty the project. See [Design-space exploration](../concepts/xplr.md).

## regression.yaml

Required keys are `rtl-buddy-filetype: reg_config` and `test-configs`:

```yaml
rtl-buddy-filetype: reg_config
test-configs:
  - design/example_block_a/verif/tests.yaml
  - design/example_block_b/verif/tests.yaml
```

Paths resolve from `regression.yaml`. Each suite keeps its own command root and artefact tree. `rb regression` filters tests with `--start-level` and `--reg-level`.

## models.yaml

Required keys are `rtl-buddy-filetype: model_config` and `models`.

```yaml
rtl-buddy-filetype: model_config
models:
  - name: my_design
    filelist: [-F my_design.f]
    spec: ../../spec/my_design/specs.yaml
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Model identifier; must be unique across every `models.yaml`, not only within one, and regardless of `graph:`. Must start with a letter, digit or underscore and contain only letters, digits, underscore, dot or hyphen |
| `filelist` | Required | Filelist entries resolved from `models.yaml` |
| `desc` | Required | Human-readable description |
| `spec` | Optional | `specs.yaml` path for `rb spec`; no simulation effect |
| `synth` | Optional | Synthesis ownership pointer, optionally with `#entry`; no current runtime consumer |
| `tests` | Optional | Test-suite ownership pointer, optionally with `#entry`; no current runtime consumer |
| `graph` | Optional | `false` opts the model out of `rb graph build`'s design tier; default `true` |
| `top` | Optional | Root module of the filelist when it is not named after the model; default `name`. Letters, digits and underscore only (no `$`), and unique across the graphable models `rb graph build` selects |

`top` is the model's root module everywhere rtl_buddy elaborates it, and it is binding, not advisory: a model has one root module, and a model whose name is not a module was already broken in every one of these flows. It roots `rb hier`, `rb hier-query`, and `rb axi-profile`, it roots the `rb graph build` design-tier export, it is the target of the graph's `model --maps_to--> module:` edge, and it is the default top of a `cdc.yaml`, `synth.yaml`, `lint.yaml`, `fpga.yaml`, `fpv.yaml`, or `mut.yaml` run against the model. Only `fpv.yaml` and `mut.yaml` have a `top:` field of their own; where one is set it wins, because a formal checker top lives in the run's own `properties:`. Setting `top` therefore changes artefact names that embed it — the FPGA bitstream is `<top>.bit`, and OpenROAD's design name follows the synthesis top.

Models in a `rb graph build` selection must not collide, and the build refuses either collision before invoking the exporter, naming both models and both `models.yaml` files.

A model name is also a directory name — `artefacts/hier/<name>/`, `artefacts/graph/design/<name>/`, and the per-model directory every flow writes — so it is restricted to a single safe path segment and rejected at load time otherwise. Path separators, absolute paths, `.` and `..` are refused.

`top` is checked at load time too, against a stricter rule: a letter or underscore, then letters, digits or underscore. It does not stay in HDL — the FPGA flows name the bitstream `<top>.bit`, and the Yosys, Vivado and OpenROAD generators interpolate it into Tcl unquoted — so a value carrying a path separator, a newline or a shell or Tcl metacharacter is refused rather than escaped per tool. That is narrower than SystemVerilog allows, deliberately: `$` is legal in an SV identifier but substitutes in Tcl, so `synth_design -top foo$bar` would elaborate a different module than the YAML names; and escaped identifiers (`\name `) can carry `/` and `;`. A top that really needs either has to be renamed, or wrapped in a module whose name does not.

**No two models may share a `name`, opted out or not.** Every per-model artefact path is keyed on it, so two exports overwrite each other in `artefacts/graph/design/<name>/` and `artefacts/hier/<name>/` while the tier reports both as built. Distinct `top:` values do not make that safe, and neither does `graph: false`: a name is also how every selector spells a model — `--model NAME`, a test's `model:`, a back-pointer — so a duplicate shadows the other entry in any lookup by name, silently. Rename one of them. A duplicate within one file is already rejected by the loader; this is the across-files half of the same rule.

**No two models that would both be exported may share a top.** `module:<top>` is a global graph id and DUT ids are never suite-qualified, so two such exports merge into a single hybrid hierarchy rather than staying apart. Give them distinct roots, or set `graph: false` on the one that is not the design of record — an opted-out model is never exported, so it claims no graph id.

Models the build is not selecting are not considered by either rule.

Set `graph: false` for a model with no elaborable root — an SV `interface` published as a library entry, or a filelist of vendored IP with no module named after the model. `rb graph build` then records the model, and every testbench and non-simulation run rooted at it, under the design tier's `skipped` list instead of attempting an export that can only fail, and removes any `artefacts/graph/design/<model>/` a previous build left behind. The config tier still emits the model node, so `spec:` and test cross-references keep resolving; it carries `graph: false` and no `maps_to` edge. The opt-out is design-tier-only: `rb hier`, `rb hier-query`, and `rb axi-profile` still run against the model and still fail if its root does not elaborate. Prefer `top:` when the filelist does elaborate and only the root module name differs.

```yaml
models:
  - name: apb_intf
    desc: APB interface library
    filelist: [-v apb_intf.sv]
    graph: false
  - name: pp_axi
    desc: Vendored AXI collection
    filelist: [-F pp_axi.f]
    top: axi_xbar
```

Filelists support `-F` recursion, `+incdir+`, `+libext+`, `+define+`, `-v`, `-y`, and source paths. Every path-valued entry, including `+incdir+` and `-y` search directories, resolves against the directory of the filelist that declares it, so a filelist pulled in with `-F` can carry the include path its own sources need. Only the simulation flow acts on that include path: the synthesis, CDC, and FPGA flows drop `+incdir+` entries when they read the generated filelist back, so a header those flows must see needs a search path configured for them instead (see [FPGA Implementation](../concepts/fpga.md)). `rb synth` likewise drops `+define+` entries and passes only the synth.yaml entry's `defines:`; it warns when the filelist carries macros it is not applying. `+define+NAME[=VALUE]` is passed as a preprocessor definition; renderer-only flows drop definitions. Multiple definitions may share one entry with `+` separators, so a value cannot contain `+`. Environment variables in entries are expanded.

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

For a plain SystemVerilog testbench, `toplevel:` names the **testbench**, not the DUT it instantiates. A `toplevel:` left over from when the field was only graph metadata and points at the DUT will compile and run, and report `NA`; see [Known Issues](../known-issues.md).

A top pinned in the builder's `compile-time` opts wins over `toplevel:`, in any spelling the family accepts — Verilator takes `--top-module`, `-top-module`, `--top`, and `-top`, and Icarus accepts the module glued to the flag (`-stb`). A disagreement between the two logs `compile.toplevel_conflict` once per run, naming both tops. SystemC and cocotb testbenches follow the same rule: those backends emit their own top flag only when the builder pins none. Families other than Verilator, VCS, and Icarus get no top flag. The flag is part of the compile fingerprint, so two testbenches over one model with different `toplevel:` no longer share a build.

Cocotb supports Verilator, Icarus, and VCS. `cocotb` must be installed and `cocotb-config` available; unsupported families or a missing `toplevel` are fatal. rtl_buddy reads `cocotb_results.xml`; cocotb tests do not need PASS/FAIL console markers.

Hooks receive the paths and variables documented in [Test plugins](../concepts/plugins.md). Generated outputs, logs, and artefacts use the directory containing `tests.yaml` as the command root; invocation cwd does not change YAML path meaning.

## synth.yaml

Required keys are `rtl-buddy-filetype: synth_config` and `syntheses`.

```yaml
rtl-buddy-filetype: synth_config
syntheses:
  - name: sky130_synth
    desc: Technology-mapped synthesis
    model: my_design
    model_path: ../src/models.yaml
    tool: yosys
    constraints: constraints.sdc
    platform: sky130hd_tt
    reglvl: 0
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Run identifier and artefact directory |
| `model` | Required | Model and elaboration top |
| `model_path` | Required | `models.yaml` path relative to `synth.yaml` |
| `tool` | Required | Backend and `cfg-synth-tools` entry |
| `desc` | Required | Human-readable description |
| `constraints` | Optional | SDC path relative to `synth.yaml` |
| `params` | Optional map | Top-level parameter overrides |
| `defines` | Optional map | Verilog preprocessor definitions |
| `platform` | Optional | `cfg-synth-platforms` entry; enables technology mapping |
| `lef-paths` / `lib-paths` | Optional lists | Block-specific LEF/Liberty files appended after platform data |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Optional map | Per-tool snake-case overrides: `synth_args`, `abc_args`, `strategy`, `frontend`, `plugin_path`, `single_unit`, `static_functions`, `conflicting_drivers` |
| `effort` | Default `standard` | `cfg-synth-efforts` entry; CLI `--effort` wins |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

`tool: yosys` writes RTLIL without a platform and a mapped netlist with one. `tool: openroad` requires platform LEF data and runs Yosys elaboration before OpenROAD timing analysis. An effort with `openroad.run: false` uses only the Yosys stage. See [Synthesis](../concepts/synthesis.md).

## synth_regression.yaml

Required keys are `rtl-buddy-filetype: synth_reg_config` and `synth-configs`:

```yaml
rtl-buddy-filetype: synth_reg_config
synth-configs: [design/example_block/synth/synth.yaml]
```

Paths resolve from the manifest. Each suite retains the command root of its `synth.yaml`; `rb synth-regression` filters entries by `--reg-level`.

## pnr.yaml

Required keys are `rtl-buddy-filetype: pnr_config` and `runs`.

```yaml
rtl-buddy-filetype: pnr_config
runs:
  - name: demo_pnr
    desc: OpenROAD place and route
    tool: openroad
    synth: demo_synth
    synth-path: ../../synth/demo/synth.yaml
    constraints: ../../synth/demo/constraints.sdc
    platform: nangate45_typ
    floorplan: {utilization: 0.55, aspect: 1.0, core-margin: 2.0}
    reglvl: 1000
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Run identifier and artefact directory |
| `tool` | Default `openroad` | Backend |
| `synth` | Required | Upstream synthesis entry |
| `synth-path` | Required | Upstream `synth.yaml`, relative to `pnr.yaml` |
| `constraints` | Required | SDC path relative to `pnr.yaml` |
| `platform` | Required | `cfg-pnr-platforms` entry |
| `desc` | Required | Human-readable description |
| `lef-paths` / `lib-paths` | Optional | Design-specific macro files relative to `pnr.yaml` |
| `floorplan.utilization` | Default 0.55 | Core utilization from 0 to 1 |
| `floorplan.aspect` | Default 1.0 | Die aspect ratio |
| `floorplan.core-margin` | Default 2.0 | Core-to-die margin in microns |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Accepted, unused | Reserved per-tool mapping |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

The run consumes `<synth dir>/artefacts/<synth>/synth_netlist.v`. The selected PDK and platform provide Liberty, LEF, site, tie/fill cells, CTS buffer, and routing layers. See [Place and Route](../concepts/pnr.md).

## power.yaml

Required keys are `rtl-buddy-filetype: power_config` and `runs`.

```yaml
rtl-buddy-filetype: power_config
runs:
  - name: demo_power
    desc: Post-route dynamic power
    tool: openroad
    mode: dynamic
    netlist-source: pnr
    pnr: demo_pnr
    pnr-path: ../../pnr/demo/pnr.yaml
    platform: nangate45_typ
    activity:
      saif: ../../verif/demo/artefacts/smoke/dump.saif
      scope: tb_top/u_dut
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Run identifier and artefact directory |
| `desc` | Required | Human-readable description |
| `tool` | Default `openroad` | Backend |
| `mode` | Default `static` | `static` or `dynamic` |
| `netlist-source` | Default `synth` | `synth` or `pnr` |
| `synth`, `synth-path` | Required for synth source | Upstream synthesis entry and YAML path |
| `pnr`, `pnr-path` | Required for P&R source | Upstream P&R entry and YAML path |
| `constraints` | Required for synth source | SDC path; for P&R source defaults to routed SDC |
| `platform` | Required | `cfg-pnr-platforms` entry |
| `activity.saif` / `.vcd` | Mutually exclusive | Activity trace path |
| `activity.scope` | Only with a trace | OpenROAD trace scope; invalid without SAIF/VCD |
| `activity.default-toggle-rate` | Default 0.1 | Synthetic toggle rate for dynamic mode without a trace |
| `activity.default-static-prob` | Default 0.5 | Synthetic static probability |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Accepted, unused | Reserved per-tool mapping |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

P&R source reads the routed ODB and estimates parasitics from global routing; synthesis source reads the generated netlist. See [Power Analysis](../concepts/power.md).

## power_regression.yaml

Required keys are `rtl-buddy-filetype: power_reg_config` and `power-configs`:

```yaml
rtl-buddy-filetype: power_reg_config
power-configs: [power/demo/power.yaml]
```

Paths resolve from the manifest. Each suite retains the command root of its `power.yaml`; `rb power-regression` filters entries by `--reg-level`.

## fpga.yaml

Required keys are `rtl-buddy-filetype: fpga_config` and `runs`.

```yaml
rtl-buddy-filetype: fpga_config
runs:
  - name: demo_fpga
    desc: Counter implementation
    model: fpga_counter
    model_path: ../src/models.yaml
    part: xc7a35tcsg324-1
    xdc: [constraints/clock.xdc]
    reglvl: 1000
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Run identifier and artefact directory |
| `desc` | Required | Human-readable description |
| `model` | Required | Model name and implementation top |
| `model_path` | Required | `models.yaml` path relative to `fpga.yaml` |
| `part` | Exactly one of part/platform | Complete device part declared in the run |
| `platform` | Exactly one of part/platform | `cfg-fpga-platforms` entry supplying the part and default XDC |
| `tool` | Default `vivado` | Registered backend: `vivado` or `openxc7`; unknown values are fatal |
| `xdc` | Default empty | Run-specific constraint paths relative to `fpga.yaml` |
| `reglvl` | Default 0 | Regression level |
| `tool_overrides` | Optional map | Backend-specific overrides keyed by tool name |
| `require-timing-met` | Default false | Fail a passing routed run when the backend explicitly reports timing unmet; no effect when timing status is unavailable |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

Setting both `part` and `platform`, or neither, is fatal. A platform requires `root_config.yaml`; its XDC files are read first and the run's files afterward.

For `openxc7`, `tool_overrides.openxc7` accepts `chipdb`, `prjxray_db`, `yosys`, `nextpnr`, `fasm2frames`, and `xc7frames2bit`. `CHIPDB` and `PRJXRAY_DB_DIR` provide the database fallbacks. The openXC7 backend accepts only Xilinx 7-series parts. See [FPGA Implementation](../concepts/fpga.md) for setup, commands, and result metrics.

## fpga_regression.yaml

Required keys are `rtl-buddy-filetype: fpga_reg_config` and `fpga-configs`:

```yaml
rtl-buddy-filetype: fpga_reg_config
fpga-configs: [fpga/counter/fpga.yaml]
```

Paths resolve from the manifest. Each suite retains the command root of its `fpga.yaml`; `rb fpga-regression` filters entries by `--reg-level`. Discovery checks `./fpga_regression.yaml` before `cfg-rtl-reg.fpga-reg-cfg-path`.

## cdc.yaml

Required keys are `rtl-buddy-filetype: cdc_config` and `analyses`.

```yaml
rtl-buddy-filetype: cdc_config
analyses:
  - name: demo_cdc
    desc: CDC analysis
    model: demo_top
    model_path: ../../design/demo/models.yaml
    tool: rtl-buddy-cdc
    constraints: demo_top.sdc
    frontend: slang
    single_unit: true
    reglvl: 0
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Analysis identifier and artefact directory |
| `model` | Required | Model and elaboration top |
| `model_path` | Required | `models.yaml` relative to `cdc.yaml` |
| `tool` | Required | Analyzer and `cfg-cdc-tools` entry |
| `constraints` | Required | SDC path relative to `cdc.yaml` |
| `desc` | Required | Human-readable description |
| `waivers` | Optional | Waiver path relative to `cdc.yaml` |
| `frontend` | Optional | Forwarded analyzer frontend |
| `single_unit` | Default false | Forward `--single-unit` for one preprocessor compilation unit |
| `blackbox` | Optional list | Module names forwarded with `--blackbox` |
| `recognized-syncs` | Optional list | Instance regular expressions accepted as synchronizers |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Optional map | Per-analyzer overrides |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

`rb cdc` produces text and JSON analyzer outputs. See the [CLI reference](cli.md) for commands and options.

## cdc_regression.yaml

Required keys are `rtl-buddy-filetype: cdc_reg_config` and `cdc-configs`:

```yaml
rtl-buddy-filetype: cdc_reg_config
cdc-configs: [lint/cdc/demo/cdc.yaml]
```

Paths resolve from the manifest. Each suite retains the command root of its `cdc.yaml`; `rb cdc-regression` filters analyses by `--reg-level`. Discovery checks `./cdc_regression.yaml` before `cfg-rtl-reg.cdc-reg-cfg-path`.

## lint.yaml

Required keys are `rtl-buddy-filetype: lint_config` and `checks`.

```yaml
rtl-buddy-filetype: lint_config
checks:
  - name: demo_style
    desc: Project style policy
    model: demo_top
    model_path: ../../design/demo/models.yaml
    exclude: ["*_csr_pkg.sv"]
    reglvl: 0
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Check identifier and artefact directory |
| `model` | Required | Model whose sources are linted |
| `model_path` | Required | `models.yaml` relative to `lint.yaml` |
| `desc` | Required | Human-readable description |
| `exclude` | Optional list | Additional `fnmatch` globs; `*` may cross `/` |
| `extra_args` | Optional list | Appended after `cfg-verible.extra_args.lint`; later duplicate flags win |
| `reglvl` | Optional | Regression level |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

Lint uses the platform-routed `cfg-verible` entry. Model expansion drops `-v`, `-y`, and `+` directives, then applies root and check exclusions. Outputs are `artefacts/<name>/lint.f` and `lint.log`. See the [CLI reference](cli.md) for commands and options.

## lint_regression.yaml

Required keys are `rtl-buddy-filetype: lint_reg_config` and `lint-configs`:

```yaml
rtl-buddy-filetype: lint_reg_config
lint-configs: [lint/style/lint.yaml]
```

Paths resolve from the manifest. `rb lint-regression` filters checks by `--reg-level`. Discovery checks `./lint_regression.yaml` before `cfg-rtl-reg.lint-reg-cfg-path`.

## fpv.yaml

Required keys are `rtl-buddy-filetype: fpv_config` and `verifications`.

```yaml
rtl-buddy-filetype: fpv_config
verifications:
  - name: demo_fpv_fifo
    desc: FIFO interface properties
    tool: sby
    model: demo_fifo
    model_path: ../../design/demo_fifo/models.yaml
    top: demo_fifo
    constraints: shared_clock_reset.sv
    properties: [demo_fifo_props.sv]
    mode: bmc
    depth: 32
    engines: [smtbmc yices]
    reglvl: 1000
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Verification identifier and artefact directory |
| `desc` | Required | Human-readable description |
| `tool` | Required | Backend and `cfg-fpv-tools` entry; only `sby` is supported |
| `model` | Required | Model name |
| `model_path` | Required | `models.yaml` relative to `fpv.yaml` |
| `top` | Default model | Elaboration top; letters, digits and underscore only (no `$`), same rule as a `models.yaml` `top` |
| `properties` | Optional | Property files relative to `fpv.yaml`; may be omitted for in-RTL FORMAL properties |
| `constraints` | Optional | One environment-assumption file, read before properties |
| `mode` | Default `bmc` | `bmc`, `prove`, `cover`, or `live` |
| `depth` | Default 20 | Proof depth |
| `engines` | Default `[smtbmc yices]` | SymbiYosys engine specifications |
| `params` | Optional map | Top-level parameter overrides applied to proof, vacuity, and COI elaboration |
| `reglvl` | Optional | Regression level |
| `covers` | Optional list | Specification coverage IDs; no proof effect |
| `tool_overrides` | Optional map | Per-tool `timeout` and `extra_args` |
| `vacuity` | Default true for bmc/prove | Derive antecedent reachability covers; default false for cover/live |
| `coi` | Default true | Run cone-of-influence and dead-assume analysis |
| `frontend` | Default `verilog` | `verilog` or `slang`; slang requires the configured plugin |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

A verification's own `top` wins over the model's, and it is checked at load time against the [same rule](#modelsyaml) a `models.yaml` `top` is: it reaches the generated yosys script (`prep -top <top>`), the `chparam` lines and the `bind_to` construction unquoted, so a path separator, a newline, a `$` or an escaped identifier is refused rather than escaped per generator. A `mut.yaml` `top` is checked the same way.

Parameter names must be identifiers. Values may be integers, booleans, or strings containing whitespace-free SystemVerilog literal text; string parameters need embedded quotes, for example `MODE: '"small"'`. YAML boolean-like keys such as unquoted `on` and invalid values are rejected. The verilog frontend uses `chparam`; slang applies `-G` during elaboration.

Design sources, constraints, and properties are read in that order. See [Formal Property Verification](../concepts/fpv.md) for frontend behavior, proof-quality checks, artefacts, and counterexamples.

## fpv_regression.yaml

Required keys are `rtl-buddy-filetype: fpv_reg_config` and `fpv-configs`:

```yaml
rtl-buddy-filetype: fpv_reg_config
fpv-configs: [design/example_block/fpv/fpv.yaml]
```

Paths resolve from the manifest. Each suite retains the command root of its `fpv.yaml`; `rb fpv-regression` filters entries by `--reg-level`.

## mut.yaml

Required keys are `rtl-buddy-filetype: mut_config`, `model`, `model_path`, `design_file`, `operators`, and `verify`.

```yaml
rtl-buddy-filetype: mut_config
model: demo_top
model_path: ../../design/demo_top/models.yaml
design_file: ../../design/demo_top/rtl/alu.sv
operators: [arith_flip, bit_op_flip, cond_negate]
verify:
  fpv_config: ../../fpv/demo/fpv.yaml
  verification: demo_fpv_alu_safety
budget:
  max_mutants: 100
  schedule: sequential
```

| Field | Requirement | Meaning |
|---|---|---|
| `model` | Required | Model name |
| `model_path` | Required | `models.yaml` relative to `mut.yaml` |
| `design_file` | Required | Baseline mutation file inside the model directory |
| `operators` | Required, non-empty | `arith_flip`, `bit_op_flip`, `cond_negate`, `cond_const`, `assign_drop`, `port_binding_swap` |
| `verify.fpv_config` / `.verification` | Pair | FPV oracle config and entry |
| `verify.test_config` | Optional | Simulation oracle suite |
| `verify.tests` | Default all | Selected simulation tests |
| `verify.assertions` | Default true | Enable Verilator assertions for simulation oracle |
| `name` | Default model | Campaign and artefact name |
| `top` | Default model | Top module the FPV oracle elaborates; letters, digits and underscore only (no `$`), same rule as a `models.yaml` `top` |
| `budget.max_mutants` | Default 100 | Global campaign cap |
| `budget.per_file_cap` | Default null | Per-scoped-file cap |
| `budget.time_budget_minutes` | Default null | Wall-clock cap |
| `budget.schedule` | Default `sequential` | `sequential` or `round_robin` |
| `scope.include` / `.exclude` | Default empty | Case-sensitive `fnmatch` globs over instance and source paths; `**` is not recursive |

A campaign's own `top` wins over both the model's and the oracle verification's, and it is applied to the baseline proof and to every mutant proof alike — the two verdicts are only comparable when elaborated from the same root module. Only the FPV oracle elaborates a top: the simulation oracle runs the suite's own testbenches, so a campaign that configures only that oracle logs `mut_config.top_override_unused` and ignores the value.

At least one oracle is required; `fpv_config` requires `verification`. Empty scope mutates `design_file` without the viewer. Non-empty scope requires `rtl-buddy-view`, selects hierarchy source files, and fails if none match. `design_file` and every scoped file must remain within the model directory. See [Mutation Testing](../concepts/mut.md).

## specs.yaml

Required keys are `rtl-buddy-filetype: spec_config` and `blocks`.

```yaml
rtl-buddy-filetype: spec_config
blocks:
  - name: my_design
    desc: Design requirements
    docs: [README.md]
    coverage-items:
      - id: MY-COV-01
        desc: Normal operation
```

| Field | Requirement | Meaning |
|---|---|---|
| `blocks[].name` | Required | Block identifier matched to model name in multi-block specs |
| `blocks[].desc` | Required | Human-readable description |
| `blocks[].docs` | Optional list | Markdown paths relative to `specs.yaml` |
| `blocks[].coverage-items` | Default empty | Functional coverage item list |
| `coverage-items[].id` | Required | Identifier used by `covers` in tests and formal verifications |
| `coverage-items[].desc` | Required | Verification requirement |

A single-block file matches its linked model unconditionally. These fields affect traceability only. See [Spec Traceability](../concepts/spec-traceability.md).
