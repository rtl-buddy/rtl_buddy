## root_config.yaml

`root_config.yaml` lives at the project root. It selects the platform, simulator, shared tools, physical-design data, regression manifests, and dispatch defaults.

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
| `cfg-platforms[].surfer` | Optional | Entry in `cfg-surfer`; `surfer-default` is used when unset |

Every routed name is validated at load time for every platform entry. CLI selections such as `--builder` and `--surfer` override platform defaults. Per-flow `cfg-*-tools` blocks are selected by the flow YAML's `tool`; they cannot be routed from `cfg-platforms`.

Executable and tool path fields accept a string or an ordered candidate list. This covers `cfg-rtl-builder[].builder`, `cfg-verible[].path`, `cfg-surfer[].path`, `cfg-systemc.home`, and `tool` in `cfg-*-tools` entries.

- `~` and environment variables are expanded.
- Relative paths anchor to `root_config.yaml`.
- The first expanded candidate that exists wins. A bare final name is resolved through `PATH`.
- A candidate containing an unset variable is skipped. If every candidate contains one, rtl_buddy warns and keeps the literal value.

Project-local environment defaults belong in [`.rtl-buddy/.env`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/root-config/#project-local-env-defaults-rtl-buddyenv).

### Simulator builders

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Builder identifier |
| `builder` | Required | Compiler executable or candidate list |
| `builder-simv` | Required | Simulation executable path relative to the build directory. An absolute path disables cross-test shared builds |
| `sim-rand-seed` | Required | Default random seed |
| `sim-rand-seed-prefix` | Required | Simulator argument prefix for the seed |
| `builder-opts.<mode>.compile-time` | Required per used mode | Compile arguments |
| `builder-opts.<mode>.run-time` | Required per used mode | Simulation arguments |
| `simulator-family` | Optional | `verilator`, `vcs`, or `icarus`; inferred from the executable when unset |
| `wave-format` | Optional | `fst-postproc` converts VCD to FST with `vcd2fst` before `rb wave`. A missing `vcd2fst` falls back to VCD |
| `extra-sim-timeout` | Optional, default 0 | Non-negative seconds added to each test timeout for this builder. CLI `--extra-sim-timeout` overrides it |

`--builder-mode` selects a `builder-opts` key. A missing mode, or a missing compile or run stage, is fatal.

`compile-time` tokens get `~` and `$VAR` expansion, like filelist entries, plus `${RTL_BUDDY_PROJECT_ROOT}`, which rtl_buddy sets to the project root. An unset variable is left as written. The compile runs from the test's artefact directory, whose depth changes under `--run-tag`, so name a project file as `${RTL_BUDDY_PROJECT_ROOT}/design/waive.vlt` instead of by a relative path. See [Simulator support](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/simulators/).

### Verible, coverage, and Surfer

| Block | Fields and behavior |
|---|---|
| `cfg-verible` | `name`, `path`; optional `extra_args` keyed by `lint`, `format`, `syntax`, or `preprocessor` (default empty); optional `exclude` globs. Configured args precede CLI args. For the active platform, an invalid configured directory warns and falls back to `PATH` when possible |
| `cfg-coverage` | `name` is the simulator family. `use-lcov: true` enables LCOV info and HTML. `merge-timeout` is the seconds the raw merge may run before it is stopped and reported as failed; unset, the default, is no limit, since a merge of a few hundred databases can take minutes |
| `cfg-coverview` | `name`, `generate-tables`, and inline Coverview `config` |
| `cfg-surfer` | `name`, `path`; optional `wcp-port` (0 asks the OS), `editor-cmd` with `%f`/`%l`, `editor-terminal` (`tmux`, `iterm2`, `terminal`, or empty), `editor-sock`, and `ctrl-sock` |

See [Coverage](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/), [Waveforms](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/wave/), and the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/) for lint commands.

### Synthesis and physical-design tools

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      synth-args: ""
      abc-args: ""
      abc-script: ""
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
| `cfg-synth-tools` | `name`, `tool`, and `opts`. Yosys options are `synth-args`, `abc-args`, `abc-script`, `frontend`, `plugin-path`, `single-unit`, `best-effort-hierarchy`, `static-functions`, `conflicting-drivers`, and `unresolved-interfaces`. OpenROAD also accepts `strategy` |
| `cfg-pdks` | `name`, `site`, `corners`; optional `tech-lef`, `macro-lef`, `cell-gds`, `klayout-tech`, `klayout-props`, `tie-hi`, `tie-lo`, `fill-cells`, `pin-layers.horizontal` / `pin-layers.vertical` (default `metal3` / `metal2`), `placement.*`, `dont-use-cells`, `pdn-config`, `rcx-rules`, `tracks-tcl`, `layer-rc-tcl`, `tapcell-tcl`, and `platform-tcl`. `corners` maps a corner name to its standard-cell Liberty: one path, or a list for cells split across files. `cell-gds` takes one path or a list. Each path resolves on its own from `root_config.yaml` |
| `cfg-synth-platforms` | `name`, `pdk`; optional `corner` (the first declared corner by default) and `dont-use-cells` |
| `cfg-pnr-platforms` | `name`, `pdk`; optional `corner` or `corners`, `cts-buffer`, `cts-sink-clustering` (default `true`), `cts-apply-ndr`, `post-cts-setup-repair` (default `false`), `global-route-hold-repair` (default `false`), `routing-layer-adjustment`, `max-fanout`, `routing-layers.signal` / `.clock`, `placement.*`, and `dont-use-cells`. `corners` is a non-empty list of PDK corner names, the first being the primary, analysed together by `rb pnr` and `rb power`; it excludes `corner`. See [multi-corner signoff](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners) |
| `cfg-synth-efforts` | Named `yosys.synth-args`, `yosys.abc-args`, `yosys.abc-script`, `openroad.run`, `openroad.pre-sta-tcl`, and `openroad.repair` settings. `openroad.repair` (default `false`) runs `repair_design` and `repair_timing -setup` before the synthesis STA reports. The built-in default is `standard`. Precedence is per-run override, then effort, then tool config |
| `cfg-pnr-tools` | `name`, `tool` |
| `cfg-power-tools` | `name`, `tool` |

`placement.*` stands for `placement.density`, `placement.padding`, `placement.macro-halo`, `placement.macro-cell-halo`, `placement.tie-separation`, and `placement.reference-hpwl`.

The process-dependent P&R keys are all optional:

| Key | Where | Behavior |
|---|---|---|
| `placement.density` | `cfg-pdks`, `cfg-pnr-platforms` | Global-placement target density, greater than 0 and at most 1. Default `0.7` |
| `placement.padding` | `cfg-pdks`, `cfg-pnr-platforms` | Global-placement cell padding in sites, a non-negative integer applied to both `-pad_left` and `-pad_right`. Default `1` |
| `placement.macro-halo` | `cfg-pdks`, `cfg-pnr-platforms` | Minimum channel in microns between two macros and between a macro and each core edge, kept by the macro packer. Non-negative; default `20.0`, which `pdngen` needs to repair a channel on sky130hd |
| `placement.macro-cell-halo` | `cfg-pdks`, `cfg-pnr-platforms` | Standard-cell keep-out in microns on every side of each placed macro, applied as a hard placement blockage. Non-negative; default `1.0`; `0` places no blockage |
| `placement.tie-separation` | `cfg-pdks`, `cfg-pnr-platforms` | Distance in microns between each constant-driven load and the tie cell `repair_tie_fanout` places for it after global placement. Non-negative; default `0` |
| `placement.reference-hpwl` | `cfg-pdks`, `cfg-pnr-platforms` | Positive number. Passed as `global_placement -reference_hpwl`. Unset by default, which keeps the placer's size-derived reference; a fixed value spreads a large design further and can clear global-route overflow |
| `dont-use-cells` | `cfg-pdks`, `cfg-synth-platforms`, `cfg-pnr-platforms` | Cell names or patterns (`*` and `?` wildcards only), one per list entry. Empty by default. See below for scope |
| `pdn-config` | `cfg-pdks` | Path to a Tcl snippet that declares the power grid. P&R sources it and calls `pdngen`. Unset by default |
| `rcx-rules` | `cfg-pdks` | Path to an OpenRCX extraction-rules file. P&R extracts the routed design, writes `<top>.routed.spef`, and times its final reports on it. A `netlist-source: pnr` power run reads that SPEF instead of estimating. Unset by default |
| `tracks-tcl` | `cfg-pdks` | Path to a Tcl script of `make_tracks` commands (ORFS `MAKE_TRACKS`). P&R sources it after `initialize_floorplan` in place of the bare `make_tracks`. Unset by default |
| `layer-rc-tcl` | `cfg-pdks` | Path to a Tcl script of `set_layer_rc` / `set_wire_rc` commands (ORFS `SET_RC_TCL`). P&R sources it after `read_sdc`, before placement-time parasitics estimates and CTS, and warns `pnr.no_wire_rc` when it is unset; a `netlist-source: pnr` power run sources it after `read_sdc` too and digests its contents when it estimates parasitics. A synthesis effort with `openroad.repair` sources it before the repair. Unset by default |
| `tapcell-tcl` | `cfg-pdks` | Path to a Tcl script that inserts tap and endcap cells (ORFS `TAPCELL_TCL`). P&R sources it after macro placement, before the power grid. Unset by default |
| `platform-tcl` | `cfg-pdks` | Path to a Tcl script that `rb pnr` and `rb power` source before reading Liberty (ORFS `PLATFORM_TCL`), such as `suppress_message` lines. Unset by default |
| `cts-buffer` | `cfg-pnr-platforms` | One buffer name or a list. A list becomes the CTS `-buf_list`, with its first entry as `-root_buf` |
| `cts-sink-clustering` | `cfg-pnr-platforms` | Boolean. Passes `-sink_clustering_enable` to `clock_tree_synthesis`. Default `true`; set `false` when CTS fails with `CTS-0080` |
| `cts-apply-ndr` | `cfg-pnr-platforms` | One of `none`, `root_only`, `half`, `full`. Passed as `clock_tree_synthesis -apply_ndr`. Unset by default, which keeps OpenROAD's default (`half`); set `none` when global route detours clock nets |
| `post-cts-setup-repair` | `cfg-pnr-platforms` | Boolean. Runs `repair_timing -setup` after CTS, before hold repair. Default `false` |
| `global-route-hold-repair` | `cfg-pnr-platforms` | Boolean. Runs `repair_timing -hold` on `estimate_parasitics -global_routing` after global route, then legalizes and reroutes incrementally before detail route. Default `false` |
| `routing-layer-adjustment` | `cfg-pnr-platforms` | Number from 0 to 1. Global-routing capacity withheld on the `routing-layers.signal` layers (`set_global_routing_layer_adjustment`). Unset by default, which keeps the router's default |
| `max-fanout` | `cfg-pnr-platforms` | Positive integer. `rb pnr` and `rb power` add `set_max_fanout <n> [current_design]` after each `read_sdc`. Unset by default, which leaves the Liberty or SDC limit, or 50 in `repair_design` when neither sets one |

A `placement:` block on a P&R platform overrides its PDK's block field by field: the platform wins where it names a value, the PDK where it does not. See [Place-and-Route](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#tune-the-process-dependent-steps).

`dont-use-cells` scope:

- The PDK's list is excluded by both synthesis and P&R. A `cfg-synth-platforms` list applies only to synthesis, and a `cfg-pnr-platforms` list only to P&R.
- A platform's list is added to its PDK's, PDK entries first with duplicates dropped. It never replaces it.
- P&R fails a run whose routed design still instantiates an excluded cell.

For synthesis, `frontend: verilog` is the default. `frontend: slang` requires `plugin-path` or `RTL_BUDDY_SLANG_PLUGIN`; relative plugin paths resolve from the project root. `single-unit` and `best-effort-hierarchy` are slang-only booleans. `best-effort-hierarchy: true` asks yosys-slang to keep module instances as hierarchy instead of inlining them, which a design that relies on `(* keep_hierarchy *)` for mapping needs. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/#systemverilog-frontend).

`abc-args` is the argument string of the `abc` command an unmapped `tool: yosys` run adds after `synth`; empty adds none. `abc-script` is the ABC script of a Liberty-mapped run's `abc -liberty` command, on both backends; `default` and `delay` name the built-in presets. Empty selects `delay`, which also omits `&dch -f`, when the run's `synth-args` pass `-extra-map +/choices/<map>`, and otherwise `default`, which omits `dc2` and `&fraig -x`. A script is one line of `;`-separated ABC commands without double quotes, and `{D}` in it takes the SDC delay target. A mapped run ignores `abc-args` and warns. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/#choose-the-mapped-run-abc-script).

In `synth.yaml` overrides, use snake-case keys such as `plugin_path` and `single_unit`. Unknown keys warn and are ignored; a non-mapping override or a wrong `single_unit` type is fatal. The elaboration override key is `yosys` for both Yosys and OpenROAD runs. An OpenROAD run's Yosys stage also reads `tool_overrides.openroad`, with `yosys` winning per key, and takes its tool options from the `yosys` entry of `cfg-synth-tools`, or from the `openroad` entry when there is no `yosys` entry. `strategy` is read only from `openroad`. Keys that no stage reads warn. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/#systemverilog-frontend).

`static-functions`, `conflicting-drivers`, and `unresolved-interfaces` are correctness gates on the Yosys elaboration stage, which the `yosys` and `openroad` backends both use. Omit an option to take its default. An unrecognized value is fatal.

| Option | Values | Default | Behavior |
|---|---|---|---|
| `static-functions` | `error`, `warn`, `allow` | `error` with `frontend: slang`, `warn` with `frontend: verilog` | Before Yosys starts, scans the filelist sources and the headers they `` `include `` for `function` or `task` declarations with no explicit `automatic` lifetime. `error` fails the run and names each `file:line: function <name>`. `warn` logs one warning per finding and records `static_function_findings` in the result envelope and machine output. `allow` skips the scan |
| `conflicting-drivers` | `error`, `allow` | `error` | After Yosys exits, fails the run if its log contains `multiple conflicting drivers` warnings, reporting the count and the log path. Warnings whose drivers are all tristate buffers and module ports are a working bus and are not counted |
| `unresolved-interfaces` | `error`, `warn`, `allow` | `warn` | After Yosys exits, reports each ``Could not find interface instance for `<inst>' in `<module>'`` warning, de-duplicated across `hierarchy` passes. `read_verilog` cannot bind an interface instance to a child's interface port, which leaves signals such as `clk` or `rst_n` undriven. `warn` logs one `synth.unresolved_interface` per instance and records `unresolved_interfaces` in the result envelope and machine output. `error` fails the run and drops the netlist. `allow` skips the scan. `frontend: slang` binds the instance and never warns |

The `static-functions` scan follows the same macro and include rules Yosys does. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/#gate-static-lifetime-subroutines).

- `` `include `` resolves against the including file's directory, then the filelist's `+incdir+` entries.
- `` `ifdef ``, `` `ifndef ``, `` `elsif ``, `` `else ``, and `` `endif `` are evaluated against the macros Yosys is given: the filelist's `+define+` entries, then the run's `defines:` (which win on conflict), plus the frontend's predefined macros. Those are `SYNTHESIS` and `YOSYS` for `read_verilog`, and `SYNTHESIS` plus slang's built-ins for `read_slang`.
- A bare `+define+X` takes the frontend's meaning of a valueless macro: empty under `read_verilog`, `1` under slang.
- A run whose `defines:` override a filelist entry logs one `synth.filelist_defines_overridden` warning naming both values.
- The macro table is reset per source by default and shared across sources when `single-unit` makes slang read them as one compilation unit.
- `` `undefineall `` follows the frontend: slang re-applies the command-line macros, `read_verilog` does not.

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
| `cfg-fpga-tools[].tool` | Required | Executable or candidate list. Relative paths anchor to `root_config.yaml` |
| `cfg-fpga-platforms[].name` | Required | Platform identifier used by `fpga.yaml` |
| `cfg-fpga-platforms[].part` | Required | Complete FPGA device part |
| `cfg-fpga-platforms[].board` | Default empty | Informational board name |
| `cfg-fpga-platforms[].package` | Default empty | Informational package name; it is not appended to `part` |
| `cfg-fpga-platforms[].xdc` | Default empty | Constraint paths relative to `root_config.yaml` |

Platform XDC files are read before a run's XDC files, so run-level constraints can override platform defaults. An unknown platform reference is fatal. See [FPGA Implementation](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/fpga/).

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

A `cfg-fpv-tools` entry has `name`, `tool`, and optional `opts.timeout`, `opts.extra-args`, `opts.plugin-path`, and `opts.solver-versions`. Solver pins are exact and a mismatch is fatal. The supported solver names are `yices`, `z3`, `boolector`, `bitwuzla`, `btormc`, and `abc`. See [Formal Property Verification](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/fpv/).

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

`cfg-tools` overrides built-in minimum versions for `rb tool-check`. A platform-qualified entry applies only to that `cfg-platforms[].os` and takes precedence over an unqualified entry. A platform name absent from `cfg-platforms` is fatal. See [Tool dependency check](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/tool-check/).

### Regression manifest defaults

`cfg-rtl-reg.reg-cfg-path` is the fallback when `regression.yaml` is absent from the current directory. Optional flow fallbacks are `elab-reg-cfg-path`, `synth-reg-cfg-path`, `power-reg-cfg-path`, `fpga-reg-cfg-path`, `cdc-reg-cfg-path`, `fpv-reg-cfg-path`, and `lint-reg-cfg-path`. Relative paths resolve from `root_config.yaml`. A root-local manifest takes precedence over its fallback.

`cfg-rtl-reg.shared-build-root` is optional and is not a manifest. It is the persistent directory that shared builds are cached under, replacing the in-tree `artefacts/.shared-builds/` so the cache survives a workspace wipe.

- Relative paths resolve from the project root. `~` and `$VAR` are expanded.
- `--shared-build-root` overrides it. `RTL_BUDDY_SHARED_BUILD_ROOT` sits between the two.
- It applies only with `--share-build`, which `--dispatch` implies.
- Enabling or disabling it recompiles each shared build once.

See [Persistent build cache](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/tests/#persistent-build-cache).

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
  coverage: {time: "01:00:00", modes: {cov: {mem: 8G}}}
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
| `backend` | `local`; values are `local`, `local-parallel`, `slurm`. Applies automatically to regression, elaboration regression, and randtest. `rb test` and `rb elab` need an explicit `--dispatch` |
| `jobs` | `min(4, CPU count)`; a positive size for the local-parallel global pool. CLI `--jobs` wins |
| `resources.cpus` | 1; positive integer |
| `resources.mem` | Optional Slurm memory value |
| `resources.time` | `"01:00:00"`; quote it. Accepted Slurm forms are minutes, `MM:SS`, `HH:MM:SS`, and `DD-HH[:MM[:SS]]`. An unquoted value that YAML parses as an integer is fatal |
| `resources.modes` | Unset; `{<builder mode>: {cpus, mem, time}}`. Per-mode reservation; see [Per-mode reservations](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#per-mode-reservations) |
| `compile` | Inherits `resources`. Reserves the build, or is folded field by field into workers that compile locally. A suite's top-level `compile:` in `tests.yaml` layers over it field by field. Where verilation is split into its own job, it sizes the C++ build job alone |
| `compile.modes` | Unset; like `resources.modes`, plus a `verilate` sub-block: `compile.modes.<mode>.verilate.{cpus,mem,time}` |
| `compile.parallel` | 1; integer of at least 1. Number of distinct builds the suite's build job compiles concurrently. A suite's own `compile.parallel` overrides it. See below |
| `compile.verilate` | `{cpus, mem, time}` sizing the verilate job of a split Verilator suite. `cpus` defaults to 2, since verilation is single-threaded. `mem` and `time` default to the resolved `compile` values. Ignored where the split does not apply |
| `compile.split-verilate` | `true`; splits a Verilator suite's build job into a verilate job and a C++ build job chained on `afterok`. A suite's own `compile.split-verilate` overrides it; Slurm only, since `local-parallel` never splits |
| `coverage` | Inherits `resources`. Reserves the coverage tail job (merge, model build, LCOV exports, manifest) that `--dispatch slurm` submits after the simulations. Takes `cpus`, `mem`, `time` and `modes`; `mem`, `time` and `cpus` must be greater than zero. See [Run the coverage tail as a job](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#run-the-coverage-tail-as-a-job) |
| `sbatch-args` | Empty list. Appended verbatim after the generated flags, so it overrides duplicates. See [`sbatch-args` behavior](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#sbatch-args-behavior) |
| `max-jobs-per-array` | Per-array Slurm throttle, not a whole-run cap |
| `max-array-size` | Unset; read from the cluster's `MaxArraySize` via `scontrol show config`. Must be at least 2. Slurm's largest task index is one below it, so `1001` allows 1000 elements per array. See [Array limits](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#array-limits) |
| `max-array-tasks` | Unset; read from the cluster's `SchedulerParameters=max_array_tasks`. Must be at least 1, and is an inclusive count of tasks per array, so `1000` allows 1000 elements. See [Array limits](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#array-limits) |
| `orphans` | `warn`; values are `warn`, `cancel`, `adopt`. What the next run does about an interrupted run's jobs that are still queued or running. CLI `--orphans` wins. See [Orphaned jobs](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#orphaned-jobs) |
| `poll-interval` | Positive seconds between backend polls |
| `progress-interval` | 60; non-negative seconds between console updates. 0 disables console progress |
| `max-wait` | Unset; positive seconds per collection round. Expiry fails the run and cancels outstanding jobs |
| `retry.attempts` | 0; extra attempts after the first |
| `retry.backoff-sec` / `backoff-max-sec` | 60 / 600; non-negative, and the maximum must not be below the initial backoff |
| `retry.jitter` | 0.5; in `[0, 1)` |
| `retry.classifiers` | `[license-queue]`; unknown classifiers are fatal |
| `rightsize.report` | true |
| `rightsize.over-threshold` / `near-limit` / `margin` | 0.5 / 0.9 / 1.5. Lower `over-threshold` to shorten the `reduce` list on a run where most tests fit |

`compile.parallel` multiplies only the build job's `cpus` reservation, capped at the suite's planned test count. `mem` and `time` are submitted as written. Above 1, the job runs every config's `preproc` before any builder starts, so no hook may change another config's inputs. It has no effect where a builder compiles inside its own simulation job, since that job is one serial build.

`parallel` and `split-verilate` are honored only in `cfg-dispatch.compile` and a suite's top-level `compile:`. In a per-test or per-testbench `resources:` block they are ignored with a warning; in a testbench `compile:` block or any `modes:` block they are rejected at load.

An unknown key in `cfg-dispatch`, in its `resources`, `compile`, `compile.verilate`, `coverage`, `retry` or `rightsize` block, in a `tests.yaml` `resources:` or `compile:` block, or in an elaboration profile's `resources` is ignored. Each one logs the warning `config.unknown_key` with the file, the block and the nearest known key, for example `did you mean 'mem'?` for `memory:`. A later major release will make it fatal.

### Per-mode reservations

A `modes:` block resizes a reservation for the run's `--builder-mode`.

- It is available on every reservation block: `cfg-dispatch.resources`, `cfg-dispatch.compile`, `cfg-dispatch.coverage`, a suite's top-level `compile:`, and a testbench's or test's `resources:` and `compile:`.
- The base value resolves first. The mode block then applies over the resolved result, least specific layer first, so any mode block beats every base field: `test.modes[m]` > `testbench.modes[m]` > `cfg-dispatch.modes[m]` > `test` > `testbench` > `cfg-dispatch`.
- `cfg-dispatch.resources.modes` also sizes the compile reservation for that mode, because `resources` is the least specific layer of `compile`. To size only the build, put the mode under `cfg-dispatch.compile.modes`.
- Within a compile block, any `verilate` key beats any `compile` key, and within each, any mode block beats every base field. `compile.modes.<mode>.verilate` is therefore the most specific verilate value.
- Omitted fields and unnamed modes inherit, so a mode that no block names reserves the base value.
- Mode names are free text, normally your `cfg-rtl-builder.builder-opts` keys, but they must be strings. Quote `on`, `no`, and `yes`.
- Fields use the base validators, including the quoted-`time` rule.
- A `modes:` block rejects `parallel`, `split-verilate`, a nested `modes:`, and unknown keys at load. A base `resources:` or `compile:` block instead ignores an unknown key after a `config.unknown_key` warning, so a misspelled field such as `memory:` reserves nothing.
- `modes:` is also rejected inside `compile.verilate` (write `compile.modes.<mode>.verilate`) and on an elaboration profile's `resources`, which resolves without a builder mode.
- A testbench's `compile.modes` is the most specific layer and is aggregated over the planned builds like the base fields.
- A mode block is not part of the compile fingerprint.

See [Size a reservation per builder mode](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#size-a-reservation-per-builder-mode).

### `sbatch-args` behavior

- The build job and the verilate job of a split suite emit their own `--dependency` after `sbatch-args`, composing your expression with the shared-build dedup. They also emit `--job-name` after it, because the dedup serialises on that name. A `--job-name` or `-J` here therefore does not rename those two jobs. It still renames simulation jobs, replacing any `RTL_BUDDY_JOB_TAG` prefix; see [Tag job names for one caller](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#tag-job-names-for-one-caller).
- An argument that sets the job's CPU request supersedes the resolved `cpus`. These are `-c` / `--cpus-per-task`, the task and node counts `-n` / `--ntasks`, `--ntasks-per-node`, and `-N` / `--nodes`, and a GPU count (`--gpus` / `-G`, `--gpus-per-node`, `--gpus-per-socket`, or a GPU `--gres`) combined with `--ntasks-per-gpu` and no `--ntasks`. The `SBATCH_NTASKS`, `SBATCH_NTASKS_PER_NODE`, and `SBATCH_NODES` environment variables count the same way, with the command line winning over the environment.
- CPU right-sizing then uses the scheduler's `ReqCPUS` for that run, and its `cpus` advice names `sbatch-args` instead of `resources.cpus` or `compile.cpus`. A direct `--cpus-per-task` also disables the compile `cpus` floor.
- Not overrides: `--threads-per-core`, `-B`, `--ntasks-per-core`, `--ntasks-per-socket`, a lone `--ntasks-per-gpu`, `--exclusive`, `--cpus-per-gpu`, and `SBATCH_CPUS_PER_TASK`.

See [Judge cpu advice against requested cpus](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#judge-cpu-advice-against-requested-cpus) for the advice text.

### Array limits

Slurm refuses an array larger than its limits, so rtl_buddy splits a larger resource group into several arrays. Each limit is read from `scontrol show config` unless set here, and the slice size is the smaller of the known limits.

- `max-array-size` and `max-array-tasks` layer independently, configured value over probed value. Setting one does not suppress the probe for the other.
- Set them where the submit host cannot run `scontrol`, or to split groups more finely. Set `max-array-tasks` where the cluster caps tasks per array below `MaxArraySize`.
- `max-array-tasks` alone still splits a group when `MaxArraySize` cannot be resolved.

### Orphaned jobs

The next run finds an interrupted run's still-live jobs from the `artefacts/.dispatch/run-<pid>-<token>.json` manifest that run's head wrote.

- `warn` names them and submits anyway.
- `cancel` runs `scancel` on them first. The cancel is verified and fatal if the jobs survive it.
- `adopt` collects them instead of submitting. It needs exactly one complete orphan whose test config, backend, tests, plan, resolved reservations, and invocation options all match this run, and is fatal otherwise, including for a record left mid-submission.
- Only scheduler-backed backends consult it. Elsewhere the value is ignored with a warning, and an explicit `--orphans adopt` is fatal.
- A coverage tail job left running is recorded in `artefacts/.dispatch/coverage/`. `cancel` cancels it; `warn` and `adopt` let it finish before this run writes `cov_dir/`.

See [Interrupted runs](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#interrupted-runs-warn-cancel-adopt).

### Backend differences

`local-parallel` ignores scheduler memory and time reservations and produces no right-sizing advice. An elaboration profile's `cpus` still sizes its pyslang worker, and `compile.parallel` still applies to simulation builds.

Retry applies only to simulation jobs with license-queue evidence. Slurm additionally requires a `TIMEOUT`, `NODE_FAIL`, or `PREEMPTED` state and a successful build. See [Parallel dispatch](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/).

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
| `source-scope` | `["."]`; a non-empty list with no blank path |
| `disk-high-watermark-gb` | 50.0; non-negative garbage-collection threshold |
| `disk-hard-cap-gb` | 80.0; non-negative and not below the high watermark |
| `eviction-policy` | `keep-frontier`; values are `keep-frontier`, `oldest-first`, and `manual` |
| `worktree-root` | `artefacts/xplr/worktrees`; non-blank. Relative paths resolve from the project root |

Unknown keys and malformed values are fatal. When `root_config.yaml` or `cfg-xplr` is absent, XPLR uses these defaults. Keep `worktree-root` under a gitignored path so experiment worktrees do not dirty the project. See [Design-space exploration](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/xplr/).
