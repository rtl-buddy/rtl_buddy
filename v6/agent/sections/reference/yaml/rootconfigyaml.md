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

Project-local environment defaults belong in [`.rtl-buddy/.env`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/root-config/#project-local-env-defaults-rtl-buddyenv).

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

`--builder-mode` selects a `builder-opts` key. A missing mode or missing compile/run stage is fatal.

`compile-time` tokens get `~` and `$VAR` expansion, like filelist entries, plus `${RTL_BUDDY_PROJECT_ROOT}`, which rtl_buddy sets to the project root. The compile runs from the test's artefact directory, whose depth changes under `--run-tag`, so name a project file as `${RTL_BUDDY_PROJECT_ROOT}/design/waive.vlt` rather than by a relative path. An unset variable is left as written. See [Simulator support](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/simulators/).

### Verible, coverage, and Surfer

| Block | Fields and behavior |
|---|---|
| `cfg-verible` | `name`, `path`; optional `extra_args` keyed by `lint`, `format`, `syntax`, or `preprocessor`, and `exclude` globs. Configured args precede CLI args. For the active platform, an invalid configured directory warns and falls back to `PATH` when possible |
| `cfg-coverage` | `name` is the simulator family; `use-lcov: true` enables LCOV info and HTML |
| `cfg-coverview` | `name`, `generate-tables`, and inline Coverview `config` |
| `cfg-surfer` | `name`, `path`; optional `wcp-port` (0 asks the OS), `editor-cmd` with `%f`/`%l`, `editor-terminal` (`tmux`, `iterm2`, `terminal`, or empty), `editor-sock`, and `ctrl-sock` |

See [Coverage](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/coverage/), [Waveforms](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/wave/), and the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/cli/) for lint commands.

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
      best-effort-hierarchy: false
      static-functions: error
      conflicting-drivers: error
      unresolved-interfaces: warn

cfg-pdks:
  - name: sky130hd
    site: unithd
    corners:
      tt: pdk/sky130hd/lib/tt.lib
    tech-lef: pdk/sky130hd/tech.lef
    macro-lef: pdk/sky130hd/macros.lef
    cell-gds:
      - pdk/sky130hd/gds/sky130_fd_sc_hd.gds
      - pdk/sky130hd/gds/sky130_fd_sc_hd_fill.gds
    klayout-tech: pdk/sky130hd/sky130hd.lyt
    placement: {density: 0.55, padding: 2, macro-halo: 30.0}
    dont-use-cells: ["*_lp__*", "sky130_fd_sc_hd__probe*"]
    pdn-config: pdk/sky130hd/pdn.tcl
    rcx-rules: pdk/sky130hd/rcx_patterns.rules

cfg-synth-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    corner: tt

cfg-pnr-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    corner: tt
    cts-buffer: [sky130_fd_sc_hd__clkbuf_4, sky130_fd_sc_hd__clkbuf_8]
    cts-sink-clustering: false
    routing-layers: {signal: met1-met5, clock: met3-met5}
    placement: {density: 0.6}
    dont-use-cells: ["sky130_fd_sc_hd__lpflow_*"]   # added to the PDK's list
```

| Block | Fields and behavior |
|---|---|
| `cfg-synth-tools` | `name`, `tool`, and `opts`. Yosys options are `synth-args`, `abc-args`, `frontend`, `plugin-path`, `single-unit`, `best-effort-hierarchy`, `static-functions`, `conflicting-drivers`, and `unresolved-interfaces`. OpenROAD additionally accepts `strategy` |
| `cfg-pdks` | `name`, `site`, `corners`; optional `tech-lef`, `macro-lef`, `cell-gds`, `klayout-tech`, `klayout-props`, `tie-hi`, `tie-lo`, `fill-cells`, `pin-layers.horizontal` / `pin-layers.vertical`, `placement.density` / `placement.padding` / `placement.macro-halo`, `dont-use-cells`, `pdn-config`, and `rcx-rules`. `cell-gds` takes one path or a list of them, each resolved on its own. Pin layers default to `metal3` / `metal2`; paths resolve from `root_config.yaml` |
| `cfg-synth-platforms` | `name`, `pdk`, optional `corner` (first declared corner by default) and `dont-use-cells` |
| `cfg-pnr-platforms` | `name`, `pdk`, optional `corner`, or `corners` (a list of PDK corner names, the first being the primary, analysed together by `rb pnr` and `rb power`; mutually exclusive with `corner`, and must not be empty). See [multi-corner signoff](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#sign-off-at-several-corners); P&R fields include `cts-buffer`, `cts-sink-clustering` (default `true`), `routing-layers.signal`/`.clock`, `placement.density` / `placement.padding` / `placement.macro-halo`, and `dont-use-cells` |
| `cfg-synth-efforts` | Named `yosys.synth-args`, `yosys.abc-args`, `openroad.run`, and `openroad.pre-sta-tcl` settings. Built-in default is `standard`. Precedence is per-run override, effort, tool config |
| `cfg-pnr-tools` | `name`, `tool` |
| `cfg-power-tools` | `name`, `tool` |

The process-dependent P&R keys are all optional, and a config that omits them gets the behaviour the flow had before they existed:

| Key | Where | Behavior |
|---|---|---|
| `placement.density` | `cfg-pdks`, `cfg-pnr-platforms` | Global-placement target density, `> 0` and `<= 1`. Default `0.7` |
| `placement.padding` | `cfg-pdks`, `cfg-pnr-platforms` | Global-placement cell padding in sites, a non-negative integer applied to both `-pad_left` and `-pad_right`. Default `1` |
| `placement.macro-halo` | `cfg-pdks`, `cfg-pnr-platforms` | Minimum channel in microns kept between two macros and between a macro and each core edge by the macro packer, a non-negative distance. Default `20.0`, which is what `pdngen` needs to repair a channel on sky130hd |
| `dont-use-cells` | `cfg-pdks`, `cfg-synth-platforms`, `cfg-pnr-platforms` | Cell names or patterns (`*` / `?` wildcards only), one per list entry. The PDK's list is excluded by both synthesis and P&R; a `cfg-synth-platforms` list only by synthesis and a `cfg-pnr-platforms` list only by P&R. A platform's list is added to its PDK's (PDK entries first, duplicates dropped), never replacing it. P&R fails a run whose routed design still instantiates an excluded cell. Empty by default |
| `pdn-config` | `cfg-pdks` | Path to a Tcl snippet that declares the power grid; P&R sources it and calls `pdngen`. Unset by default |
| `rcx-rules` | `cfg-pdks` | Path to an OpenRCX extraction-rules file. P&R extracts the routed design, writes `<top>.routed.spef` and times its final reports on it; a `netlist-source: pnr` power run reads that SPEF instead of estimating. Unset by default |
| `cts-buffer` | `cfg-pnr-platforms` | One buffer name or a list of them. A list becomes the CTS `-buf_list`, with its first entry as `-root_buf` |

A `placement:` block on a P&R platform overrides its PDK's field by field: the platform wins where it names a value, the PDK where it does not. See [Place-and-Route](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#tune-the-process-dependent-steps).

For synthesis, `frontend: verilog` is the default. `frontend: slang` requires `plugin-path` or `RTL_BUDDY_SLANG_PLUGIN`; relative plugin paths resolve from the project root. `single-unit` and `best-effort-hierarchy` are slang-only and must be booleans; `best-effort-hierarchy: true` asks yosys-slang to keep module instances as hierarchy instead of inlining them, which a design relying on `(* keep_hierarchy *)` for mapping needs. In `synth.yaml` overrides, use snake-case keys such as `plugin_path` and `single_unit`; unknown keys warn and are ignored, while a non-mapping override or wrong `single_unit` type is fatal. The elaboration override key is `yosys` for both Yosys and OpenROAD runs. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/#systemverilog-frontend).

`static-functions`, `conflicting-drivers`, and `unresolved-interfaces` are correctness gates on the Yosys elaboration stage, which both the `yosys` and the `openroad` backend use. Omit an option to take its default:

| Option | Values | Default | Behavior |
|---|---|---|---|
| `static-functions` | `error`, `warn`, `allow` | `error` with `frontend: slang`, `warn` with `frontend: verilog` | Before Yosys starts, scans the filelist's sources and the headers they `` `include ``, for `function`/`task` declarations with no explicit `automatic` lifetime. `error` fails the run and names each `file:line: function <name>`; `warn` logs one warning per finding and records `static_function_findings` in the result envelope and machine output; `allow` skips the scan |
| `conflicting-drivers` | `error`, `allow` | `error` | After Yosys exits, fails the run when the log contains Yosys `multiple conflicting drivers` warnings, reporting the count and the log path. Warnings whose drivers are all tristate buffers and module ports are a working multi-driver bus and are not counted |
| `unresolved-interfaces` | `error`, `warn`, `allow` | `warn` | After Yosys exits, reports each ``Could not find interface instance for `<inst>' in `<module>'`` warning, de-duplicated across the repeated `hierarchy` passes. `read_verilog` cannot bind an interface instance to a child's interface port and falls back to per-child `<child>$interfaces$<interface>` modules, which drops the instance's own port connections — an interface carrying `clk` or `rst_n` leaves them undriven. `warn` logs one `synth.unresolved_interface` per instance and records `unresolved_interfaces` in the result envelope and machine output; `error` fails the run and drops the netlist; `allow` skips the scan. `frontend: slang` binds the instance and never emits the warning |

The scan resolves `` `include `` against the including file's directory and then the filelist's `+incdir+` entries, and evaluates `` `ifdef ``/`` `ifndef ``/`` `elsif ``/`` `else ``/`` `endif `` against exactly the macros Yosys is given: the filelist's `+define+` entries, then the run's `defines:` (which win on conflict), plus what the selected frontend predefines — `SYNTHESIS` and `YOSYS` for `read_verilog`, `SYNTHESIS` and slang's built-ins for `read_slang`. A bare `+define+X` takes the frontend's meaning of a valueless macro (empty under `read_verilog`, `1` under slang). A run whose `defines:` override a filelist entry logs one `synth.filelist_defines_overridden` warning naming both values. The macro table follows `single-unit`: reset per source by default, shared across sources when slang reads them as one compilation unit. `` `undefineall `` follows the frontend too — slang re-applies the command-line macros, `read_verilog` does not. An unrecognized value for any of these options is fatal. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/#gate-static-lifetime-subroutines).

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

Platform XDC files are read before a run's XDC files, so run-level constraints can override platform defaults. An unknown platform reference is fatal. See [FPGA Implementation](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/fpga/).

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

`cfg-fpv-tools` entries contain `name`, `tool`, and optional `opts.timeout`, `opts.extra-args`, `opts.plugin-path`, and `opts.solver-versions`. Solver pins are exact; supported names are `yices`, `z3`, `boolector`, `bitwuzla`, `btormc`, and `abc`. A mismatch is fatal. See [Formal Property Verification](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/fpv/).

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

`cfg-tools` overrides built-in minimum versions for `rb tool-check`. A platform-qualified entry applies only to that `cfg-platforms[].os` and takes precedence over an unqualified entry. A platform name absent from `cfg-platforms` is fatal. See [Tool dependency check](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tool-check/).

### Regression manifest defaults

`cfg-rtl-reg.reg-cfg-path` is the fallback when `regression.yaml` is absent from the current directory. Optional flow fallbacks are `elab-reg-cfg-path`, `synth-reg-cfg-path`, `power-reg-cfg-path`, `fpga-reg-cfg-path`, `cdc-reg-cfg-path`, `fpv-reg-cfg-path`, and `lint-reg-cfg-path`. Relative paths resolve from `root_config.yaml`. A root-local manifest takes precedence over its fallback.

`cfg-rtl-reg.shared-build-root` is optional and is not a manifest: it is the persistent directory shared builds are cached under, replacing the in-tree `artefacts/.shared-builds/` so the cache survives a workspace wipe. Relative paths resolve from the project root; `~` and `$VAR` are expanded. `--shared-build-root` overrides it, and `RTL_BUDDY_SHARED_BUILD_ROOT` sits between the two. It applies only with `--share-build` (which `--dispatch` implies), and enabling or disabling it recompiles each shared build once. See [Persistent build cache](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#persistent-build-cache).

### Parallel dispatch

```yaml
cfg-dispatch:
  backend: slurm
  jobs: 4
  resources:
    cpus: 2
    mem: 4G
    time: "01:00:00"
    modes:
      cov: {mem: 32G, time: "02:00:00"}
  compile: {cpus: 8, mem: 16G, time: "02:00:00", parallel: 4, split-verilate: true, verilate: {cpus: 2}, modes: {cov: {mem: 48G}}}
  sbatch-args: [--partition=verif]
  max-jobs-per-array: 200
  max-array-size: 1001
  max-array-tasks: 1000
  poll-interval: 10
  progress-interval: 60
  max-wait: 7200
  orphans: warn
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
| `backend` | `local`; values are `local`, `local-parallel`, `slurm`. Applies automatically to regression, elaboration regression, and randtest; `rb test` and `rb elab` require an explicit `--dispatch` |
| `jobs` | `min(4, CPU count)`; positive local-parallel global pool size; CLI `--jobs` wins |
| `resources.cpus` | 1; positive integer |
| `resources.mem` | Optional Slurm memory value |
| `resources.time` | `"01:00:00"`; quote it. Accepted Slurm forms are minutes, `MM:SS`, `HH:MM:SS`, and `DD-HH[:MM[:SS]]`; an integer from YAML sexagesimal parsing is fatal |
| `resources.modes` | Unset; `{<builder mode>: {cpus, mem, time}}`, applied over the fully resolved base value for the run's `--builder-mode`, least specific layer first, so any mode block beats every base field (`test.modes[m]` > `testbench.modes[m]` > `cfg-dispatch.modes[m]` > `test` > `testbench` > `cfg-dispatch`). Available on every reservation block: this one, `compile`, a suite's top-level `compile:`, and a testbench's or test's `resources:` and `compile:`. Omitted fields and unnamed modes inherit, so a mode no block names reserves the base value. Mode names are free text — your `cfg-rtl-builder.builder-opts` keys — but must be strings, so quote `on`/`no`/`yes`. Fields go through the same validators as the base ones, including the quoted-`time` rule. `parallel`, `split-verilate`, a nested `modes:`, and any unknown key are rejected at load, unlike an unknown key beside them in `resources:`; this block reaches the compile reservation too, since `resources` is its least specific layer |
| `compile.modes` | Unset; `resources.modes` plus a `verilate` sub-block, so `compile.modes.<mode>.verilate.{cpus,mem,time}` sizes the verilate job of a split suite under that mode. Any `verilate` key beats any `compile` key and, within each, any mode block beats every base field. A testbench's `compile.modes` is the most specific layer and is aggregated over the planned builds like the base fields. `modes:` is rejected inside `compile.verilate` — write `compile.modes.<mode>.verilate` — and on an elaboration profile's `resources`, which resolves without a builder mode. Not part of the compile fingerprint |
| `compile` | Inherits `resources`; reservation for the build, or folded field-by-field into workers that compile locally. Where verilation is split into its own Slurm job it sizes the C++ build job alone and `compile.verilate` sizes the other. A suite's own top-level `compile:` block in `tests.yaml` layers over this field by field, `parallel` and `split-verilate` included. Those two blocks are the only ones that take `parallel` or `split-verilate`; both keys are meaningless in a per-test or per-testbench `resources:` block and are discarded there |
| `compile.parallel` | 1; integer, must be at least 1. Distinct builds the suite's build job compiles concurrently. Multiplies only that job's `cpus` reservation, capped at the suite's planned test count; `mem` and `time` are submitted as written. Above 1 the job runs every config's `preproc` before any builder starts, so no hook may mutate another config's inputs. Overridden by a suite's own `compile.parallel` where that suite sets one. Inert where a builder compiles inside its own simulation job, since one such job is one serial build |
| `compile.verilate` | `{cpus, mem, time}` sizing the verilate job of a split Verilator suite. `cpus` defaults to 2, since verilation is single-threaded; `mem` and `time` default to the resolved `compile` values. Layers field by field over the same three layers as the rest of `compile`, including a testbench's own `compile.verilate`, and is aggregated over a suite's distinct builds by the same rules. Ignored where the split does not apply |
| `compile.split-verilate` | `true`; splits a Verilator suite's build job into a verilate job and a C++ build job chained on `afterok`. A suite's own `compile.split-verilate` overrides it; a testbench block rejects the key, as it rejects `parallel`. Slurm only — `local-parallel` never splits |
| `sbatch-args` | Empty list; appended verbatim and therefore overrides duplicate generated flags. Any argument here that sets the job's cpu request — `-c`/`--cpus-per-task`, or the task/node counts that raise it (`-n`/`--ntasks`, `--ntasks-per-node`, `-N`/`--nodes`) — supersedes the resolved `cpus`, so CPU right-sizing falls back to the scheduler's `ReqCPUS` for that run and its `cpus` advice names this key rather than the masked `resources.cpus` / `compile.cpus`. Within one option the last occurrence wins, as it does for sbatch; distinct options combine instead, and the advice then names them all and leaves the combining rule to sbatch rather than claiming a product. Only a lone `-c`/`--cpus-per-task` is offered the suggested value; the task/node counts are told to be decomposed. A direct `--cpus-per-task` override also disables the compile `cpus` floor, which bounds a reservation sbatch never saw; a task or node count leaves that flag in force, so the floor is kept. The `SBATCH_NTASKS`, `SBATCH_NTASKS_PER_NODE` and `SBATCH_NODES` environment variables count the same way, since the submit inherits them (command line beats environment, and the environment is never sanitized). A GPU count (`--gpus`/`-G`, `--gpus-per-node`, `--gpus-per-socket`, a gpu `--gres`, or their `SBATCH_*` forms) together with `--ntasks-per-gpu` and no `--ntasks` also counts, since sbatch derives the task count from that pair. Node-selection constraints (`--threads-per-core`, `-B`), placement maxima (`--ntasks-per-core`, `--ntasks-per-socket`, and `--ntasks-per-gpu` on its own), `--exclusive` and `SBATCH_CPUS_PER_TASK` are not overrides — the generated `--cpus-per-task` still states the request; `--cpus-per-gpu` is not either, since Slurm rejects it alongside the `--cpus-per-task` every job carries. Two exceptions to "appended last", on the build job and on the verilate job of a split suite: each one's `--dependency` is emitted after these and composes the configured expression with the shared-build dedup (and, for the build job, with its gate on the verilate job), and each one's `--job-name` is emitted after these because that name is what the dedup serialises on — a `--job-name` / `-J` here therefore does not rename either job (it still renames simulation jobs) |
| `max-jobs-per-array` | Per-array Slurm throttle, not a whole-run cap |
| `max-array-size` | Unset; the cluster's Slurm `MaxArraySize`, read from `scontrol show config` when unset. Setting it does not suppress the probe: the probe is the only source of `max-array-tasks`, which still applies. Must be at least 2. Slurm's largest array task index is one **below** it, so `1001` allows 1000 elements per array; a resource group larger than that is split across several arrays instead of being refused by sbatch. Set it where the submit host cannot run `scontrol`, or to split groups more finely |
| `max-array-tasks` | Unset; the cluster's `SchedulerParameters=max_array_tasks`, read from `scontrol show config` when unset. Must be at least 1. Unlike `max-array-size` it is an inclusive **count** of the tasks one array may hold, so `1000` allows 1000 elements. Set it where the submit host cannot run `scontrol` and the cluster caps tasks-per-array below `MaxArraySize`. Each ceiling layers independently — configured value over probed value — and the slice size is the smaller of whichever are known, so this field alone still splits a group when `MaxArraySize` cannot be resolved |
| `orphans` | `warn`; values are `warn`, `cancel`, `adopt`. What the next run does about an interrupted run's jobs that are still queued or running, found from the `artefacts/.dispatch/run-<pid>-<token>.json` manifest the interrupted head wrote: name them and submit anyway, `scancel` them first (verified, and fatal if they survive it), or collect them instead of submitting. CLI `--orphans` wins. `adopt` needs exactly one complete matching orphan — same test config, backend, expanded tests in the same order, an identical plan down to plusdefines and the resolved seeds, the same resolved per-job reservation (so a changed `cfg-dispatch.resources` refuses), and the same invocation options (`--builder-mode`, `--builder`, `--extra-sim-timeout`, shared-build root, `--rebuild`) — and is fatal otherwise, including for a record left mid-submission. Only consulted for a scheduler-backed backend; elsewhere the value is ignored with a warning, and an explicit `--orphans adopt` is fatal |
| `poll-interval` | Positive seconds between backend polls |
| `progress-interval` | 60; non-negative seconds between console updates; 0 disables console progress |
| `max-wait` | Unset; positive seconds per collection round. Expiry fails the run and cancels outstanding jobs |
| `retry.attempts` | 0; extra attempts after the first |
| `retry.backoff-sec` / `backoff-max-sec` | 60 / 600; non-negative and max must not be below initial backoff |
| `retry.jitter` | 0.5; must be in `[0, 1)` |
| `retry.classifiers` | `[license-queue]`; unknown classifiers are fatal |
| `rightsize.report` | true |
| `rightsize.over-threshold` / `near-limit` / `margin` | 0.5 / 0.9 / 1.5; lower `over-threshold` to shorten the `reduce` list on a run where most tests fit |

Local-parallel ignores scheduler memory/time reservations and produces no right-sizing advice; an elaboration profile's `cpus` still sizes its pyslang worker, and `compile.parallel` still applies to simulation builds as concurrency inside the build job. Retry applies only to simulation jobs with license-queue evidence; Slurm additionally requires `TIMEOUT`, `NODE_FAIL`, or `PREEMPTED` and a successful build. See [Parallel dispatch](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/dispatch/).

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

Unknown keys and malformed values are fatal. When `root_config.yaml` or `cfg-xplr` is absent, XPLR uses these defaults. Keep `worktree-root` under a gitignored path so experiment worktrees do not dirty the project. See [Design-space exploration](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/xplr/).
