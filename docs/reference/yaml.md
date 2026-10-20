---
description: Canonical field reference for rtl_buddy project, model, elaboration, test, regression, implementation, FPGA, formal, lint, CDC, XPLR, mutation, and specification YAML files.
---

# YAML Formats

Use this page for required keys, defaults, path resolution, and validation. Use the linked concept pages for procedures and interpretation.

Unless stated otherwise:

- Relative paths resolve from the YAML file that contains them. See [Execution Context](../concepts/execution-context.md).
- `reglvl` defaults to 0. It is an integer, or a per-tool or per-builder map with a `default` fallback. A run is selected when its level is at most the CLI regression level.
- `xfail: true` is non-strict; `xfail_strict: true` makes an unexpected pass fail. Neither excuses a failure that happened instead of a verdict, such as a setup or compile failure, a sim timeout, or a lost dispatch job. `xfail_match: "<regex>"` names the expected failure, so a run that fails for another reason stays `FAIL`. See [Expected failures](../concepts/expected-failures.md).
- Unknown references and invalid required combinations fail during configuration loading.
- A key written twice in one mapping fails during configuration loading, naming the file, the key and both line numbers. Plain YAML keeps only the last value. A key set after a `<<:` merge overrides the merged value and is not a duplicate.

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

Project-local environment defaults belong in [`.rtl-buddy/.env`](../concepts/root-config.md#project-local-env-defaults-rtl-buddyenv).

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

`compile-time` tokens get `~` and `$VAR` expansion, like filelist entries, plus `${RTL_BUDDY_PROJECT_ROOT}`, which rtl_buddy sets to the project root. An unset variable is left as written. The compile runs from the test's artefact directory, whose depth changes under `--run-tag`, so name a project file as `${RTL_BUDDY_PROJECT_ROOT}/design/waive.vlt` instead of by a relative path. See [Simulator support](../concepts/simulators.md).

### Verible, coverage, and Surfer

| Block | Fields and behavior |
|---|---|
| `cfg-verible` | `name`, `path`; optional `extra_args` keyed by `lint`, `format`, `syntax`, or `preprocessor` (default empty); optional `exclude` globs. Configured args precede CLI args. For the active platform, an invalid configured directory warns and falls back to `PATH` when possible |
| `cfg-coverage` | `name` is the simulator family. `use-lcov: true` enables LCOV info and HTML. `merge-timeout` is the seconds the raw merge may run before it is stopped and reported as failed; unset, the default, is no limit, since a merge of a few hundred databases can take minutes |
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
      abc-script: ""
      frontend: verilog
      plugin-path: ""
      single-unit: false
      best-effort-hierarchy: false
      static-functions: error
      conflicting-drivers: error
      unresolved-interfaces: warn
      clock-gating-min-flops: 0
      abc-persistent: true

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
| `cfg-synth-tools` | `name`, `tool`, and `opts`. Yosys options are `synth-args`, `abc-args`, `abc-script`, `frontend`, `plugin-path`, `single-unit`, `best-effort-hierarchy`, `static-functions`, `conflicting-drivers`, `unresolved-interfaces`, `clock-gating-min-flops`, and `abc-persistent`. OpenROAD also accepts `strategy` |
| `cfg-pdks` | `name`, `site`, `corners`; optional `tech-lef`, `macro-lef`, `cell-gds`, `klayout-tech`, `klayout-props`, `tie-hi`, `tie-lo`, `fill-cells`, `pin-layers.horizontal` / `pin-layers.vertical` (default `metal3` / `metal2`), `placement.*`, `dont-use-cells`, `pdn-config`, `rcx-rules`, `tracks-tcl`, `layer-rc-tcl`, `tapcell-tcl`, and `platform-tcl`. `corners` maps a corner name to its standard-cell Liberty: one path, or a list for cells split across files. `cell-gds` takes one path or a list. Each path resolves on its own from `root_config.yaml` |
| `cfg-synth-platforms` | `name`, `pdk`; optional `corner` (the first declared corner by default), `dont-use-cells`, `abc-driving-cell` (the Liberty cell ABC assumes drives every primary input), `abc-output-load` (femtofarads on every primary output) and `clock-gating-cell` (`'<cell> <enable>:<clock>:<gated clock>'`, the ICG `clockgate` uses instead of choosing one from the Liberty). The two `abc-*` keys are written to `abc.constr` and passed as `abc -constr` on mapped runs; see [Synthesis](../concepts/synthesis.md#give-abc-a-driving-cell-and-output-load) and [clock gating](../concepts/synthesis.md#gate-enable-flops-with-clock-gating-cells) |
| `cfg-pnr-platforms` | `name`, `pdk`; optional `corner` or `corners` (a list, first is primary, excludes `corner`), `routing-layers.signal` / `.clock`, `placement.*`, `dont-use-cells`, and the P&R keys below. See [Place-and-Route](../concepts/pnr.md#configure-the-physical-platform) |
| `cfg-synth-efforts` | Named `yosys.synth-args`, `yosys.abc-args`, `yosys.abc-script`, `yosys.clock-gating-min-flops`, `openroad.run`, `openroad.pre-sta-tcl`, and `openroad.repair` settings. `openroad.repair` (default `false`) runs `repair_design` and `repair_timing -setup` before the synthesis STA reports. The built-in default is `standard`. Precedence is per-run override, then effort, then tool config |
| `cfg-pnr-tools` | `name`, `tool` |
| `cfg-power-tools` | `name`, `tool` |

An unknown key in a `cfg-pdks` or `cfg-pnr-platforms` entry, including its `pin-layers`, `routing-layers` and `placement` blocks, is a config error that names the key, the entry and the valid keys. It stops every command that loads `root_config.yaml`. A `corners` mapping takes any corner name.

`placement.*` stands for every `placement.` key in the table below.

P&R keys, all optional. A key set at more than one level takes its value from the most specific: the PDK, then the platform, then the `pnr.yaml` run. `placement.*` and `routing-layers.*` go field by field. A path resolves from the file that sets it. When to use each is in [Tune placement, clock tree and repair](../concepts/pnr.md#tune-placement-clock-tree-and-repair).

| Key | Where | Behavior |
|---|---|---|
| `placement.density` | PDK, platform | Global-placement target density, above 0 and at most 1. Default `0.7` |
| `placement.padding` | PDK, platform | Cell padding in sites on each side. Default `1` |
| `placement.macro-halo` | PDK, platform | Channel in µm the packer keeps between macros and from the core edge. Default `20.0`; below about 19 fails `PDN-0179` on sky130hd |
| `placement.macro-cell-halo` | PDK, platform | Standard-cell keep-out in µm around each macro. Default `1.0`; `0` disables |
| `placement.tie-separation` | PDK, platform | Distance in µm between each constant-driven load and its own tie cell. Default `0` |
| `placement.reference-hpwl` | PDK, platform | Positive number passed as `global_placement -reference_hpwl`. Unset keeps the placer's own |
| `placement.routability-driven` | PDK, platform | `true` adds `-routability_driven`. Default `false` |
| `placement.routability-use-grt` | PDK, platform | `true` adds `-routability_use_grt`. Needs `routability-driven: true` after overrides. Default `false` |
| `placement.timing-driven` | PDK, platform | `true` adds `-timing_driven`; on OpenROAD 26Q4 it also buffers and resizes during placement. Default `false` |
| `placement.rotated-macro-footprint` | PDK, platform | `block` (default), `diamond-legalizer` or `none`. See [Known Issues](../known-issues.md#openroads-legalizer-blocks-a-rotated-macros-unrotated-footprint) |
| `dont-use-cells` | PDK, synth and P&R platforms | Cell names or globs (`*`, `?`), one per entry. Empty by default |
| `pdn-config` | PDK | Tcl that declares the power grid; the flow calls `pdngen`. Unset means no grid |
| `rcx-rules` | PDK | OpenRCX rules. Enables extraction, `<top>.routed.spef` and `post-route-repair` |
| `layer-rc-tcl` | PDK | `set_layer_rc` / `set_wire_rc` script, sourced after `read_sdc` by `rb pnr`, a `netlist-source: pnr` power run and a synthesis effort with `openroad.repair`. Unset warns `pnr.no_wire_rc` |
| `platform-tcl` | PDK | Sourced before the Liberty reads of `rb pnr` and `rb power` |
| `tracks-tcl` | PDK | `make_tracks` script, replacing the bare `make_tracks` |
| `tapcell-tcl` | PDK, platform, run | Tap and endcap insertion, after macro placement |
| `routing-layers.signal`, `routing-layers.clock` | platform, run | Layer ranges for `set_routing_layers -signal` / `-clock` |
| `cts-buffer` | platform, run | One cell or a list; a list's first entry is the root buffer |
| `cts-clock-nets` | run | One net or a list: `clock_tree_synthesis -clk_nets`, building trees for those clock nets only. Unset lets CTS find every clock |
| `cts-latency-balancing` | platform, run | `false` adds `-no_insertion_delay`: CTS does not pad shallow branches of a clock net with delay buffers to match its deepest. Default `true` |
| `cts-repair-clock-nets` | platform, run | `true` runs `repair_clock_nets` right after CTS, before the `after-cts` hooks. Default `false` |
| `cts-sink-clustering` | platform | `-sink_clustering_enable` for CTS. Default `true` |
| `cts-apply-ndr` | platform | `none`, `root_only`, `half` or `full`. Unset keeps OpenROAD's `half` |
| `port-buffer` | platform, run | Cell `buffer_ports` inserts. Unset lets OpenROAD choose |
| `post-cts-setup-repair` | platform, run | `true` runs `repair_timing -setup` after CTS. Default `false` |
| `global-route-hold-repair` | platform, run | `true` is `global-route-repair: {hold-margin: 0}`; not with `global-route-repair` at the same level. On a run, `false` drops the hold repair from the platform's block, and turns the block off when nothing is left. Default `false` |
| `global-route-repair` | platform, run | Repair after global route. See below |
| `post-route-repair` | platform, run | Repair rounds after detailed route. See below |
| `routing-layer-adjustment` | platform, run | 0 to 1: signal-layer capacity withheld from the global router. Unset keeps the router's default |
| `global-route` | platform, run | `global_route` options. See below |
| `max-fanout` | platform | Positive integer; `set_max_fanout` after each `read_sdc` in `rb pnr` and `rb power` |

`global-route-repair` and `post-route-repair` take:

| Key | Behavior |
|---|---|
| `repair-design` | `repair_design` with `slew-margin`, `cap-margin` (percent) and `max-wire-length` (µm); `{}` uses OpenROAD's defaults |
| `setup-margin`, `hold-margin` | `repair_timing -setup` / `-hold` with that margin in ns, at least 0 |
| `reroute` | `incremental` (default) or `full` for `global-route-repair`; `eco` (default) or `full` for `post-route-repair` |
| `rounds` | `post-route-repair` only. Positive, default 1; above 1 needs a margin |
| `margin-step` | `post-route-repair` only. ns added to the margins each round after the first. Default 0 |

The block a run ends up with must set at least one of `repair-design`, `setup-margin` and `hold-margin`. A run's block overrides the platform's field by field; `null` turns a field or the block off. See [Repair timing after routing](../concepts/pnr.md#repair-timing-after-routing).

`global-route` takes the keys below; a run's block overrides the platform's field by field, and an unset key keeps OpenROAD's default. A full global route is the flow's first, a `reroute: full` global-route repair's and a `reroute: full` post-route round's. See [Tune global routing](../concepts/pnr.md#tune-global-routing).

| Key | Behavior |
|---|---|
| `congestion-iterations` | Positive integer: `-congestion_iterations` of every full global route. Default `20` |
| `congestion-report-iter-step` | Non-negative integer: `-congestion_report_iter_step` of every full global route |
| `grid-origin` | `[x, y]` in DBU: `-grid_origin` of every `global_route` call, incremental ones included. After a repair that ends on an incremental route the flow writes and reads back the guides, which rebuilds the GCell grid at the origin |
| `layer-adjustment` | Map of routing layer to 0–1: `set_global_routing_layer_adjustment <layer>` after the all-layers `routing-layer-adjustment` |
| `critical-nets-percentage` | 0 to 100: `-critical_nets_percentage` of every full global route |
| `resistance-aware` | `true` adds `-resistance_aware` to every full global route |
| `allow-congestion` | `true` adds `-allow_congestion` to every `global_route` call. Diagnosis only: the run is never reported as a clean pass, and cannot be `harden: true` |

`dont-use-cells` scope: the PDK's list applies to synthesis and P&R, a synth or P&R platform's list is added to it for that flow only, and P&R fails a run whose routed design still uses an excluded cell.

For synthesis, `frontend: verilog` is the default. `frontend: slang` requires `plugin-path` or `RTL_BUDDY_SLANG_PLUGIN`; relative plugin paths resolve from the project root. `single-unit` and `best-effort-hierarchy` are slang-only booleans. `best-effort-hierarchy: true` asks yosys-slang to keep module instances as hierarchy instead of inlining them, which a design that relies on `(* keep_hierarchy *)` for mapping needs. See [Synthesis](../concepts/synthesis.md#systemverilog-frontend).

`abc-args` is the argument string of the `abc` command an unmapped `tool: yosys` run adds after `synth`; empty adds none. `abc-script` is the ABC script of a Liberty-mapped run's `abc -liberty` command, on both backends; `default` and `delay` name the built-in presets. Empty selects `delay`, which also omits `&dch -f`, when the run's `synth-args` pass `-extra-map +/choices/<map>`, and otherwise `default`, which omits `dc2` and `&fraig -x`. A script is one line of `;`-separated ABC commands without double quotes, and `{D}` in it takes the SDC delay target. A mapped run ignores `abc-args` and warns. See [Synthesis](../concepts/synthesis.md#choose-the-mapped-run-abc-script).

`clock-gating-min-flops` (default `0`, off) makes a Liberty-mapped run insert integrated clock-gating cells with Yosys `clockgate` after `synth` and `chformal -remove`, before `dfflibmap`, for each set of at least that many rising-edge flops sharing a clock and enable. An unmapped run warns `synth.clock_gating_ignored` and gates nothing. See [Synthesis](../concepts/synthesis.md#gate-enable-flops-with-clock-gating-cells).

`abc-persistent` (default `true`) lets Yosys reuse one `yosys-abc` process for every ABC call. `false` runs each call as its own `yosys-abc` process, through a link `yosys-tmp/abc-batch` to the same binary, on both backends; the mapping is unchanged. A run without `yosys-abc` next to its Yosys or on `PATH` is fatal. The setting joins the options fingerprint only when `false`. See [Synthesis](../concepts/synthesis.md#run-abc-in-batch-mode).

In `synth.yaml` overrides, use snake-case keys such as `plugin_path` and `single_unit`. Unknown keys warn and are ignored; a non-mapping override or a wrongly typed value, such as a quoted `single_unit` or `abc_persistent`, is fatal. The elaboration override key is `yosys` for both Yosys and OpenROAD runs. An OpenROAD run's Yosys stage also reads `tool_overrides.openroad`, with `yosys` winning per key, and takes its tool options from the `yosys` entry of `cfg-synth-tools`, or from the `openroad` entry when there is no `yosys` entry. `strategy` is read only from `openroad`. Keys that no stage reads warn. See [Synthesis](../concepts/synthesis.md#systemverilog-frontend).

`static-functions`, `conflicting-drivers`, and `unresolved-interfaces` are correctness gates on the Yosys elaboration stage, which the `yosys` and `openroad` backends both use. Omit an option to take its default. An unrecognized value is fatal.

| Option | Values | Default | Behavior |
|---|---|---|---|
| `static-functions` | `error`, `warn`, `allow` | `error` with `frontend: slang`, `warn` with `frontend: verilog` | Before Yosys starts, scans the filelist sources and the headers they `` `include `` for `function` or `task` declarations with no explicit `automatic` lifetime. `error` fails the run and names each `file:line: function <name>`. `warn` logs one warning per finding and records `static_function_findings` in the result envelope and machine output. `allow` skips the scan |
| `conflicting-drivers` | `error`, `allow` | `error` | After Yosys exits, fails the run if its log contains `multiple conflicting drivers` warnings, reporting the count and the log path. Warnings whose drivers are all tristate buffers and module ports are a working bus and are not counted |
| `unresolved-interfaces` | `error`, `warn`, `allow` | `warn` | After Yosys exits, reports each ``Could not find interface instance for `<inst>' in `<module>'`` warning, de-duplicated across `hierarchy` passes. `read_verilog` cannot bind an interface instance to a child's interface port, which leaves signals such as `clk` or `rst_n` undriven. `warn` logs one `synth.unresolved_interface` per instance and records `unresolved_interfaces` in the result envelope and machine output. `error` fails the run and drops the netlist. `allow` skips the scan. `frontend: slang` binds the instance and never warns |

The `static-functions` scan follows the same macro and include rules Yosys does. See [Synthesis](../concepts/synthesis.md#gate-static-lifetime-subroutines).

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

A `cfg-fpv-tools` entry has `name`, `tool`, and optional `opts.timeout`, `opts.extra-args`, `opts.plugin-path`, and `opts.solver-versions`. Solver pins are exact and a mismatch is fatal. The supported solver names are `yices`, `z3`, `boolector`, `bitwuzla`, `btormc`, and `abc`. See [Formal Property Verification](../concepts/fpv.md).

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

`cfg-rtl-reg.reg-cfg-path` is the fallback when `regression.yaml` is absent from the current directory. Optional flow fallbacks are `elab-reg-cfg-path`, `synth-reg-cfg-path`, `power-reg-cfg-path`, `fpga-reg-cfg-path`, `cdc-reg-cfg-path`, `fpv-reg-cfg-path`, and `lint-reg-cfg-path`. Relative paths resolve from `root_config.yaml`. A root-local manifest takes precedence over its fallback.

`cfg-rtl-reg.shared-build-root` is optional and is not a manifest. It is the persistent directory that shared builds are cached under, replacing the in-tree `artefacts/.shared-builds/` so the cache survives a workspace wipe.

- Relative paths resolve from the project root. `~` and `$VAR` are expanded.
- `--shared-build-root` overrides it. `RTL_BUDDY_SHARED_BUILD_ROOT` sits between the two.
- It applies only with `--share-build`, which `--dispatch` implies.
- Enabling or disabling it recompiles each shared build once.

See [Persistent build cache](../concepts/tests.md#persistent-build-cache).

`cfg-rtl-reg.shared-build-prune` is a boolean, default `false`. With shared builds enabled, `true` removes known Verilator intermediates after a new successful compile and stamp write, while holding the build lock. It applies to both in-tree and persistent shared builds. It does not clean cache hits, unshared builds, other simulators, or verilate-only jobs. The executable and its stamp remain reusable; a subsequent recompile regenerates all removed intermediates. See [Pruning Verilator intermediates](../concepts/tests.md#pruning-verilator-intermediates) for preserved assets, diagnostics and debugging tradeoffs.

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
| `resources.modes` | Unset; `{<builder mode>: {cpus, mem, time}}`. Per-mode reservation; see [Per-mode reservations](#per-mode-reservations) |
| `compile` | Inherits `resources`. Reserves the build, or is folded field by field into workers that compile locally. A suite's top-level `compile:` in `tests.yaml` layers over it field by field. Where verilation is split into its own job, it sizes the C++ build job alone |
| `compile.modes` | Unset; like `resources.modes`, plus a `verilate` sub-block: `compile.modes.<mode>.verilate.{cpus,mem,time}` |
| `compile.parallel` | 1; integer of at least 1. Number of distinct builds the suite's build job compiles concurrently. A suite's own `compile.parallel` overrides it. See below |
| `compile.verilate` | `{cpus, mem, time}` sizing the verilate job of a split Verilator suite. `cpus` defaults to 2, since verilation is single-threaded. `mem` and `time` default to the resolved `compile` values. Ignored where the split does not apply |
| `compile.serialise-checkouts` | `false`; with a shared-build cache root, names a suite's Slurm build and verilate jobs after its cache directory, so checkouts sharing the cache queue behind each other on `singleton` instead of racing. Project-wide only; inert without a cache root or off Slurm. See [dispatch](../concepts/dispatch.md) |
| `compile.split-verilate` | `true`; splits a Verilator suite's build job into a verilate job and a C++ build job chained on `afterok`. A suite's own `compile.split-verilate` overrides it; Slurm only, since `local-parallel` never splits |
| `coverage` | Inherits `resources`. Reserves the coverage tail job (merge, model build, LCOV exports, manifest) that `--dispatch slurm` submits after the simulations. Takes `cpus`, `mem`, `time` and `modes`; `mem`, `time` and `cpus` must be greater than zero. See [Run the coverage tail as a job](../concepts/dispatch.md#run-the-coverage-tail-as-a-job) |
| `sbatch-args` | Empty list. Appended verbatim after the generated flags, so it overrides duplicates. See [`sbatch-args` behavior](#sbatch-args-behavior) |
| `max-jobs-per-array` | Per-array Slurm throttle, not a whole-run cap |
| `max-array-size` | Unset; read from the cluster's `MaxArraySize` via `scontrol show config`. Must be at least 2. Slurm's largest task index is one below it, so `1001` allows 1000 elements per array. See [Array limits](#array-limits) |
| `max-array-tasks` | Unset; read from the cluster's `SchedulerParameters=max_array_tasks`. Must be at least 1, and is an inclusive count of tasks per array, so `1000` allows 1000 elements. See [Array limits](#array-limits) |
| `orphans` | `warn`; values are `warn`, `cancel`, `adopt`. What the next run does about an interrupted run's jobs that are still queued or running. CLI `--orphans` wins. See [Orphaned jobs](#orphaned-jobs) |
| `poll-interval` | Positive seconds between backend polls |
| `progress-interval` | 60; non-negative seconds between console updates. 0 disables console progress |
| `max-wait` | Unset; positive seconds per collection round. Expiry fails the run and cancels outstanding jobs. For `rb pnr --dispatch slurm`, how long each stage job may wait in the queue before it starts |
| `retry.attempts` | 0; extra attempts after the first |
| `retry.backoff-sec` / `backoff-max-sec` | 60 / 600; non-negative, and the maximum must not be below the initial backoff |
| `retry.jitter` | 0.5; in `[0, 1)` |
| `retry.classifiers` | `[license-queue]`; unknown classifiers are fatal |
| `rightsize.report` | true |
| `rightsize.over-threshold` / `near-limit` / `margin` | 0.5 / 0.9 / 1.5. Lower `over-threshold` to shorten the `reduce` list on a run where most tests fit |

`compile.parallel` multiplies only the build job's `cpus` reservation, capped at the suite's planned test count. `mem` and `time` are submitted as written. Above 1, the job runs every config's `preproc` before any builder starts, so no hook may change another config's inputs. It has no effect where a builder compiles inside its own simulation job, since that job is one serial build.

`parallel` and `split-verilate` are honored only in `cfg-dispatch.compile` and a suite's top-level `compile:`. In a per-test or per-testbench `resources:` block they are ignored with a warning; in a testbench `compile:` block or any `modes:` block they are rejected at load.

An unknown key in `cfg-dispatch`, in its `resources`, `compile`, `compile.verilate`, `coverage`, `retry` or `rightsize` block, in a `tests.yaml` `resources:` or `compile:` block, or in an elaboration profile's `resources` is ignored. Each one logs the warning `config.unknown_key` with the file, the block and the nearest known key, for example `did you mean 'mem'?` for `memory:`. A later major release will make it fatal. A key written twice in the same block is fatal, not a warning.

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

See [Size a reservation per builder mode](../concepts/dispatch.md#size-a-reservation-per-builder-mode).

### `sbatch-args` behavior

- The build job and the verilate job of a split suite emit their own `--dependency` after `sbatch-args`, composing your expression with the shared-build dedup. They also emit `--job-name` after it, because the dedup serialises on that name. A `--job-name` or `-J` here therefore does not rename those two jobs. It still renames simulation jobs, replacing any `RTL_BUDDY_JOB_TAG` prefix; see [Tag job names for one caller](../concepts/dispatch.md#tag-job-names-for-one-caller).
- An argument that sets the job's CPU request supersedes the resolved `cpus`. These are `-c` / `--cpus-per-task`, the task and node counts `-n` / `--ntasks`, `--ntasks-per-node`, and `-N` / `--nodes`, and a GPU count (`--gpus` / `-G`, `--gpus-per-node`, `--gpus-per-socket`, or a GPU `--gres`) combined with `--ntasks-per-gpu` and no `--ntasks`. The `SBATCH_NTASKS`, `SBATCH_NTASKS_PER_NODE`, and `SBATCH_NODES` environment variables count the same way, with the command line winning over the environment.
- CPU right-sizing then uses the scheduler's `ReqCPUS` for that run, and its `cpus` advice names `sbatch-args` instead of `resources.cpus` or `compile.cpus`. A direct `--cpus-per-task` also disables the compile `cpus` floor.
- Not overrides: `--threads-per-core`, `-B`, `--ntasks-per-core`, `--ntasks-per-socket`, a lone `--ntasks-per-gpu`, `--exclusive`, `--cpus-per-gpu`, and `SBATCH_CPUS_PER_TASK`.

See [Judge cpu advice against requested cpus](../concepts/dispatch.md#judge-cpu-advice-against-requested-cpus) for the advice text.

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

See [Interrupted runs](../concepts/dispatch.md#interrupted-runs-warn-cancel-adopt).

### Backend differences

`local-parallel` ignores scheduler memory and time reservations and produces no right-sizing advice. An elaboration profile's `cpus` still sizes its pyslang worker, and `compile.parallel` still applies to simulation builds.

Retry applies only to simulation jobs with license-queue evidence. Slurm additionally requires a `TIMEOUT`, `NODE_FAIL`, or `PREEMPTED` state and a successful build. See [Parallel dispatch](../concepts/dispatch.md).

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
    elaborations:
      - name: smoke
        defines: {CHECKS_ENABLED: 1}
        parameters: {DATA_WIDTH: 32}
        resources: {cpus: 2, mem: 2G, time: "00:10:00"}
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Model identifier. Unique across every `models.yaml`, regardless of `graph:`. See [Model names](#model-names) |
| `filelist` | Required | Filelist entries resolved from `models.yaml`. See [Filelists](#filelists) |
| `desc` | Optional | Human-readable description |
| `spec` | Optional | `specs.yaml` path for `rb spec`; no simulation effect |
| `axi_bundles` | Optional | `axi-bundles.yaml` path relative to `models.yaml`, written by `rb axi-profile discover` |
| `axi_monitor_out` | Optional | Path relative to `models.yaml` where `rb axi-profile gen-monitor` writes the monitor |
| `cdc` | Optional | `cdc.yaml` path relative to `models.yaml`, optionally with `#analysis_name`. `rb hub` reads it for the clock-domain overlay |
| `synth` | Optional | Synthesis ownership pointer, optionally with `#entry`; no current runtime consumer |
| `tests` | Optional | Test-suite ownership pointer, optionally with `#entry`; no current runtime consumer |
| `graph` | Optional | `false` opts the model out of the `rb graph build` design tier. Default `true` |
| `top` | Optional | Root module of the filelist when it is not named after the model. Default `name`. See [Model top](#model-top) |
| `elaborations` | Optional, default empty | Named pyslang profile deltas used by `rb elab --profile` and `rb elab-regression`. The model stays directly elaborable without them |

### Model names

A model name is also a directory name (`artefacts/hier/<name>/`, `artefacts/graph/design/<name>/`, and the per-model directory every flow writes). It is checked at load time: it must start with a letter, digit, or underscore, and contain only letters, digits, underscore, dot, or hyphen. Path separators, absolute paths, `.`, and `..` are refused.

No two models may share a `name`, including models with `graph: false`. Per-model artefact paths and every selector (`--model NAME`, a test's `model:`, a back-pointer) are keyed on the name, so a duplicate silently shadows the other entry. A duplicate within one file is rejected by the loader. `rb graph build` refuses a duplicate across the files it selects before invoking the exporter, naming both models and both `models.yaml` files; rename one entry.

### Model top

`top` is the model's root module in every flow and is binding, not advisory. It roots `rb hier`, `rb hier-query`, `rb axi-profile`, and the `rb graph build` design-tier export, and it is the target of the graph's `model --maps_to--> module:` edge. It is also the default top of a `cdc.yaml`, `synth.yaml`, `lint.yaml`, `fpga.yaml`, `fpv.yaml`, or `mut.yaml` run against the model.

- Only `fpv.yaml` and `mut.yaml` have a `top:` field of their own; where one is set it wins, because a formal checker top lives in the run's own `properties:`.
- Setting `top` changes artefact names that embed it. The FPGA bitstream is `<top>.bit`, and OpenROAD's design name follows the synthesis top.
- `top` is checked at load time: a letter or underscore, then letters, digits, or underscores. It is interpolated unquoted into Tcl and used in artefact names, so a path separator, newline, `$`, shell or Tcl metacharacter, or SystemVerilog escaped identifier is refused rather than escaped per tool. Rename such a module, or wrap it in one with a plain name.

### Graph opt-out and top collisions

Models that `rb graph build` would both export must not share a `top`. `module:<top>` is a global graph id and DUT ids are not suite-qualified, so two such exports merge into one hybrid hierarchy. The build refuses this before invoking the exporter and names both models and both `models.yaml` files. Give the models distinct roots, or set `graph: false` on the one that is not the design of record. Models outside the build's selection are not considered.

Set `graph: false` for a model with no elaborable root, such as an SV `interface` published as a library entry or a filelist of vendored IP with no module named after the model.

- `rb graph build` records the model, and every testbench and non-simulation run rooted at it, in the design tier's `skipped` list. It also removes any `artefacts/graph/design/<model>/` left by an earlier build.
- The config tier still emits the model node, so `spec:` and test cross-references resolve. The node carries `graph: false` and no `maps_to` edge.
- The opt-out affects only the design tier. `rb hier`, `rb hier-query`, and `rb axi-profile` still run against the model and fail if its root does not elaborate.
- Prefer `top:` when the filelist elaborates and only the root module name differs.

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

### Filelists

Filelists support `-F` recursion, `+incdir+`, `+libext+`, `+define+`, `-v`, `-y`, and source paths. Environment variables in entries are expanded.

- Every path-valued entry, including `+incdir+` and `-y` directories, resolves against the directory of the filelist that declares it. A filelist pulled in with `-F` can therefore carry the include path its own sources need.
- `+define+NAME[=VALUE]` declares a preprocessor macro. Several may share one entry separated by `+`, so a value cannot contain `+`.

Flows differ in which entries they honor:

- Simulation and model elaboration apply `+incdir+` and `+define+`.
- Synthesis (`read_verilog -I` and `read_slang -I`, on both yosys backends), FPGA (openXC7's `read_verilog -I`, Vivado's `synth_design -include_dirs`), and the Vivado CDC tool forward every `+incdir+`, each directory resolved against the filelist that declared it.
- `rb cdc` and the hub's domain-map build use rtl-buddy-cdc, which has no include-path option. The run logs `cdc.filelist_incdirs_unsupported` naming the directories, and the analyzer's own `Cannot find include file` error follows if a header resolves only through them.
- `rb synth` applies `+define+` entries. The `synth.yaml` entry's `defines:` win on conflict and log `synth.filelist_defines_overridden`.
- Renderer-only flows drop definitions.

### Elaboration profiles

A profile is a delta on its containing model, not another model reference. Paths in `prepend_sources`, `append_sources`, and `include_dirs` resolve from `models.yaml`. Top precedence is profile `top`, then model `top`, then model `name`. Profile names are unique ignoring case, and every case variant of `base` is reserved so artifact paths stay portable to case-insensitive filesystems.

| Field | Default and validation |
|---|---|
| `name` | Required, unique within the model, and a safe single path segment. `base` is reserved for the bare-model artifact |
| `desc` | Optional description |
| `top` | Model top; optional simple SystemVerilog identifier |
| `reglvl` | 0; non-negative integer used by `elab-regression` |
| `prepend_sources` / `append_sources` | Empty; source or filelist entries placed before or after the model's expanded filelist |
| `include_dirs` | Empty; extra include directories placed before the model filelist |
| `defines` | Empty map of identifier to string, integer, boolean, or null. Booleans render as `1`/`0`; null defines only the name. String values cannot be empty or contain whitespace or `+`. Profile definitions win over same-named definitions in the model filelist |
| `parameters` | Empty map of top-level parameter overrides. String values are SystemVerilog expression text; booleans render as `1`/`0`. Unknown and local parameter names fail elaboration |
| `vcs_compat` | false; enables slang VCS compatibility mode |
| `single_unit` | false; parses primary sources as one compilation unit |
| `libraries_inherit_macros` | false; requires `single_unit: true` and shares primary-unit macros with library sources |
| `timescale` | Unset; command-line timescale such as `1ns/1ps` |
| `max_parse_depth` | Unset, so slang's 1024-level parser nesting limit applies. An integer from 1 to 65536; raise it for generated RTL with a deeply nested single expression, such as a long conditional or concatenation chain |
| `ignored_directives` | Empty; directive names for slang to ignore |
| `warnings` | Empty; warning controls without the `-W` prefix, such as `all`, `none`, `no-unused`, or `error=unused`. They cannot suppress hard compilation errors |
| `resources` | Inherits `cfg-dispatch.resources` field by field. `cpus` must be positive and also sets pyslang worker threads |

`rb elab MODEL -c models.yaml` runs the base model. `rb elab MODEL --profile NAME -c models.yaml` applies one profile. Outputs are `artefacts/elab/<model>/<base-or-profile>/elab.f`, `elab.log`, and `result.json`. See [Model Elaboration](../concepts/elaboration.md).

## elab_regression.yaml

The manifest lists model configuration files and runs every named profile they contain. Bare models are not run as implicit profiles.

```yaml
rtl-buddy-filetype: elab_reg_config
model-configs:
  - design/core/models.yaml
  - design/peripherals/models.yaml
```

- `model-configs` must be non-empty, and its paths resolve from the manifest.
- Duplicate paths, and profiles that would share an artifact directory, are rejected. Path comparison is case-insensitive.
- The selected files must contain at least one profile.
- `rb elab-regression` applies `--reg-level` and records higher-level profiles as `SKIP`.
- Discovery checks `./elab_regression.yaml` before `cfg-rtl-reg.elab-reg-cfg-path`.

## tests.yaml

Required top-level keys are `rtl-buddy-filetype: test_config`, `testbenches`, and `tests`. Optional top-level `builder` selects the suite default, optional top-level `compile-opts` adds compile options for one simulator family, optional top-level `filelist-modes` adds compile inputs for one builder mode, and optional top-level `compile` sizes this suite's dispatched build jobs.

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
| `compile-opts` | Optional | Compile options per simulator family, optionally per builder mode, appended after the builder mode's `compile-time`. See below |
| `filelist-modes` | Optional | Filelist entries per builder mode, compiled for every testbench in that mode only. See [Per-mode compile inputs](#per-mode-compile-inputs) |
| `compile` | Optional | This suite's whole-job dispatch compile reservation: `cpus`, `mem`, quoted `time`, `parallel`, `split-verilate`, a `verilate` sub-block, and a [`modes`](#per-mode-reservations) sub-block. See below |
| `preproc-sets-plusdefines` | Default true | Suite default for the test field of the same name |

`compile-opts` maps a simulator family (`verilator`, `vcs`, `icarus`, or a builder's `simulator-family`) to options for every test in the suite:

```yaml
compile-opts:
  verilator: "-MAKEFLAGS OPT_FAST=-O0"
  vcs: [-kdb]
```

A string splits on whitespace as `compile-time` does; a list is taken token by token. Only the family of the builder doing the compile applies, so `--builder vcs` on the suite above passes no Verilator option. The options come after the builder mode's, get the same expansion (see [Simulator builders](#simulator-builders)), and are part of the compile command, so they are part of the shared-build key. Use them for a choice that belongs to one suite, such as a lower optimisation level for a testbench whose generated code is slow to compile and quick to run; keep project-wide options in `cfg-rtl-builder`.

A family's value may instead map builder modes (the `builder-opts` keys `--builder-mode` names) to options, with an optional `default`:

```yaml
compile-opts:
  verilator:
    default: "-MAKEFLAGS OPT_FAST=-O0"
    nightly: ""        # no suite option under -M nightly
```

The compiling mode takes its own entry, else `default`, else no options. An entry present but empty means no options, not `default`. Each entry is a string or a list, as above. Mode names match exactly. The selected options are part of the compile command and so of that mode's shared-build key; a mode that selects no options builds with the same key as a suite without `compile-opts`. A string or list value applies in every mode.

The suite `compile` block layers field by field over `cfg-dispatch.compile`, which layers over `cfg-dispatch.resources`. A testbench's own `compile` overrides it per build, and omitted fields inherit. It sizes the suite's build jobs and the compile half of a simulation job that compiles for itself.

The suite value is the floor for both the verilate job and the build job of a split suite; `split-verilate: false` runs one build job instead. It is not part of the compile fingerprint, so it never invalidates a shared build stamp.

Testbench fields:

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Testbench identifier |
| `filelist` | Required | Sources appended to the model filelist |
| `filelist-modes` | Optional | Filelist entries per builder mode, compiled for this testbench in that mode only. See [Per-mode compile inputs](#per-mode-compile-inputs) |
| `resources` | Optional | Dispatch `cpus`, `mem`, and quoted `time`, plus a [`modes`](#per-mode-reservations) sub-block. Inherited by tests |
| `compile` | Optional | This testbench's per-build dispatch compile reservation. See below |
| `toplevel` | Required for cocotb and SystemC, optional otherwise | Module the compile elaborates from. Not defaulted to `name`. See [Elaboration top](#elaboration-top) |
| `cocotb.module` | Required for cocotb | Python module name or list, passed as `COCOTB_TEST_MODULES` |

A testbench `compile` holds `cpus`, `mem`, quoted `time`, a `verilate` sub-block of the same three fields, and a [`modes`](#per-mode-reservations) sub-block.

- It layers field by field over the suite's `compile`, then `cfg-dispatch.compile`. Every field must be greater than zero.
- `parallel` and `split-verilate` are rejected here and inside any `modes` block, because both are job-wide.
- The suite's build job aggregates these blocks over the builds its plan runs (largest `cpus`, summed `mem` across overlapping builds, `time` as the makespan of a `parallel`-worker queue) and floors the result at the suite-level value. Each phase of a split build aggregates its own fields the same way. A simulation job that compiles for itself uses its own testbench's value.

See [Set compile resources per suite and testbench](../concepts/dispatch.md#set-compile-resources-per-suite-and-testbench) for the aggregation rules.

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
| `resources` | Optional | Per-test dispatch reservation layered over testbench and root defaults; quote `time`. A [`modes`](#per-mode-reservations) sub-block is the most specific such layer |
| `assertions` | Default false | Enables Verilator `--assert` and user coverage. Other builders warn and ignore it |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |
| `xfail_match` | Optional regex | The failure an `xfail`/`xfail_strict` marker expects; a `FAIL` that does not match stays `FAIL`. Requires a marker |

<a id="selecting-the-simulator-builder"></a>

Builder precedence is CLI `--builder`, test `builder`, suite `builder`, then the active platform default. A `reglvl` map resolves against the effective builder.

Coverage processing uses the platform-selected builder unless `--builder` is supplied. If a suite or test overrides the builder, pass `--builder` on coverage runs so simulation and coverage select the same family.

### Per-mode compile inputs

`filelist-modes` maps a builder mode (the `builder-opts` key `--builder-mode` names), or `default`, to extra filelist entries compiled only in that mode. It is accepted at the suite top level, for every testbench, and on a testbench:

```yaml
filelist-modes:                  # suite level: every testbench
  cov: [cov_waivers.vlt]

testbenches:
  - name: tb_a
    filelist: [tb_a.sv]
    filelist-modes:              # this testbench only
      cov: [a_scope.vlt]         # scopes coverage under -M cov; -M reg never sees it
```

Each level takes the compiling mode's entry, else its `default`, else nothing; an entry present but empty selects nothing. Mode names match exactly. Entries take the same filelist syntax as `filelist` (sources, `.vlt` files, `+incdir+`, `+define+`, `-v`, `-y`, `-F`) and resolve against the suite directory. They follow the testbench `filelist` in `run.f`, suite entries before the testbench's.

The entries are compile inputs of that mode's build only, so they are in that mode's shared-build key and in no other's: adding a `cov` entry leaves the `reg` build and its key unchanged. Entries are not keyed by simulator family, so a mode that more than one builder defines passes them to each; name a Verilator-only file under a mode only Verilator builders use. Options per mode go in [`compile-opts`](#testsyaml) instead. Tools that read a testbench's sources without compiling it, such as the hierarchy view and the waveform source resolver, read only `filelist`.

### Elaboration top

<a id="pinning-the-elaboration-top"></a>

A testbench `toplevel:` roots the compile at that module. It is passed as Verilator `--top-module`, VCS `-top`, or Icarus `-s`, and to cocotb as `COCOTB_TOPLEVEL`. Other simulator families get no top flag. It is not defaulted to the testbench `name`, which is a config label rather than a module.

- Without `toplevel:`, the simulator elects a top from filelist order. Verilator takes the first ordinary (non-`-v`) entry, so recomposing a model filelist renames the model and every emitted C++ file, and an ordinary input carrying a module nothing instantiates fails the build with `MULTITOP`.
- With `toplevel:`, a testbench missing from the composed filelist fails at compile.
- For a plain SystemVerilog testbench, `toplevel:` names the testbench, not the DUT it instantiates. A `toplevel:` that points at the DUT compiles and runs but reports `NA`; see [Known Issues](../known-issues.md).
- A top pinned in the builder's `compile-time` opts wins, in any spelling the family accepts. Verilator takes `--top-module`, `-top-module`, `--top`, and `-top`, and Icarus accepts the module glued to the flag (`-stb`). A disagreement logs `compile.toplevel_conflict` once per run, naming both tops. SystemC and cocotb follow the same rule.
- The flag is part of the compile fingerprint, so two testbenches over one model with different `toplevel:` values do not share a build.

### cocotb, hooks, and command root

Cocotb supports Verilator, Icarus, and VCS. `cocotb` must be installed and `cocotb-config` available. An unsupported family or a missing `toplevel` is fatal. rtl_buddy reads `cocotb_results.xml`, so cocotb tests do not need PASS/FAIL console markers.

Hooks receive the paths and variables documented in [Test plugins](../concepts/plugins.md). Generated outputs, logs, and artefacts use the directory containing `tests.yaml` as the command root. The invocation cwd does not change YAML path meaning.

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
| `lef-paths` / `lib-paths` | Optional lists | Block-specific LEF and Liberty files, appended after platform data |
| `blocks` | Optional list | Hardened blocks the design instances. Each has `name` (the module), `pnr` (a `harden: true` P&R run), and `pnr-path` (its `pnr.yaml`, relative to `synth.yaml`); all three are required. The abstract's `.lib` and `.lef` are appended to `lib-paths` and `lef-paths`. The model's filelist must still leave the module a blackbox. See [Assemble hardened blocks](../concepts/pnr.md#assemble-hardened-blocks) |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Optional map | Per-tool snake-case overrides: `synth_args`, `abc_args`, `abc_script`, `strategy`, `frontend`, `plugin_path`, `single_unit`, `best_effort_hierarchy`, `static_functions`, `conflicting_drivers`, `unresolved_interfaces`, `clock_gating_min_flops` |
| `effort` | Default `standard` | `cfg-synth-efforts` entry. CLI `--effort` wins |
| `threads` | Default unset (1) | OpenROAD worker threads for the `tool: openroad` timing stage, as in `pnr.yaml`. No effect on Yosys. See [OpenROAD threads](../concepts/pnr.md#openroad-threads) |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |
| `xfail_match` | Optional regex | The failure an `xfail`/`xfail_strict` marker expects; a `FAIL` that does not match stays `FAIL`. Requires a marker |

`tool: yosys` writes RTLIL without a platform and a mapped netlist with one. `tool: openroad` requires platform LEF data and runs Yosys elaboration before OpenROAD timing analysis. An effort with `openroad.run: false` uses only the Yosys stage. See [Synthesis](../concepts/synthesis.md).

## synth_regression.yaml

Required keys are `rtl-buddy-filetype: synth_reg_config` and `synth-configs`:

```yaml
rtl-buddy-filetype: synth_reg_config
synth-configs: [design/example_block/synth/synth.yaml]
```

Paths resolve from the manifest. Each suite keeps the command root of its `synth.yaml`. `rb synth-regression` filters entries by `--reg-level`.

## pnr.yaml

Required keys are `rtl-buddy-filetype: pnr_config` and `runs`. Paths are relative to `pnr.yaml`. How to use the fields is in [Place-and-Route](../concepts/pnr.md).

An unknown key in a run or any mapping inside it is a config error naming the file, run, position, the nearest valid key and every valid key, for example `pnr run 'top': floorplan.blockages[2]: unknown key 'lifetme', did you mean 'lifetime'?`. All unknown keys are reported at once.

```yaml
rtl-buddy-filetype: pnr_config
runs:
  - name: demo_pnr
    desc: OpenROAD place and route
    synth: demo_synth
    synth-path: ../../synth/demo/synth.yaml
    constraints: ../../synth/demo/constraints.sdc
    platform: nangate45_typ
    floorplan: {utilization: 0.55, aspect: 1.0, core-margin: 2.0}
    lef-paths: [../../pdk/sram/sram.lef]
    gds-paths: [../../pdk/sram/sram.gds]
    reglvl: 1000
```

| Field | Default | Meaning |
|---|---|---|
| `name` | Required | Run name and artefact directory |
| `desc` | Required | Description |
| `synth`, `synth-path` | Required | Upstream synthesis entry and its `synth.yaml`. The run reads `artefacts/<synth>/synth_netlist.v` |
| `constraints` | Required | SDC file, or on a multi-corner platform a map of each corner to its SDC file. A map that misses a corner or names another fails setup. See [Sign off at several corners](../concepts/pnr.md#sign-off-at-several-corners) |
| `platform` | Required | `cfg-pnr-platforms` entry |
| `tool` | `openroad` | Only `openroad` runs; anything else is `SKIP` |
| `lef-paths`, `lib-paths` | — | Extra macro LEF and Liberty |
| `gds-paths` | — | Macro layouts for stream-out |
| `gds-mode` | `preview` | `strict` fails the run when a requested export is incomplete. `--gds-mode` overrides. See [Stream out GDS and PNG](../concepts/pnr.md#stream-out-gds-and-png) |
| `gds-allow-empty` | — | Cell names or case-sensitive globs that have no layout on purpose |
| `pin-constraints` | — | Tcl sourced just before pin placement |
| `hooks` | — | Map of hook point to a Tcl path or list. See [Run Tcl hooks](../concepts/pnr.md#run-tcl-hooks) |
| `pdn-config` | PDK's | Tcl that replaces the PDK's `pdn-config` |
| `pdn` | — | Core grid in YAML; see below |
| `placement` | Platform's | Any `placement.*` key, overriding the platform for this run |
| `routing-layers`, `routing-layer-adjustment`, `cts-buffer`, `cts-clock-nets`, `cts-latency-balancing`, `cts-repair-clock-nets`, `post-cts-setup-repair`, `global-route-hold-repair`, `port-buffer`, `tapcell-tcl` | Platform's | Override the platform's value for this run. See [P&R keys](#synthesis-and-physical-design-tools) |
| `global-route` | Platform's | Overrides the platform's block field by field. See [P&R keys](#synthesis-and-physical-design-tools) |
| `global-route-repair`, `post-route-repair` | Platform's | Overrides the platform's block field by field; `null` turns it off. See [Repair timing after routing](../concepts/pnr.md#repair-timing-after-routing) |
| `mode` | `closure` | `probe` skips timing repair and stops after global route. Not with `harden: true`. See [Probe a floorplan](../concepts/pnr.md#probe-a-floorplan) |
| `fail-on-electrical` | `false` | `true` fails a routed run with any max-slew, max-capacitance or max-fanout violator |
| `threads` | 1 | Positive integer, or `auto` for the CPUs of the current allocation. See [OpenROAD threads](../concepts/pnr.md#openroad-threads) |
| `detailed-route-verbose` | 1 | `detailed_route -verbose` level; 0 silences the iteration log |
| `checkpoints` | `false` | `true`, a stage, or a list of `floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `post_route_repair`. `[]` writes only the progress log. See [Keep stage checkpoints](../concepts/pnr.md#keep-stage-checkpoints) |
| `stages` | `false` | `true` runs each stage in its own session. A `checkpoints` list may then leave out only `post_route_repair`. See [Run stages as separate sessions](../concepts/pnr.md#run-stages-as-separate-sessions) |
| `resources` | — | `cpus`, `mem`, quoted `time`, and `stages: {<stage>: {...}}`, for `--dispatch slurm` only. See [Dispatch the stages to Slurm](../concepts/pnr.md#dispatch-the-stages-to-slurm) |
| `harden` | `false` | Publish the routed block as an abstract. Needs a single-corner platform. See [Harden a block](../concepts/pnr.md#harden-a-block) |
| `abstract` | — | Needs `harden`. `bloat`: `occupied` (default) or a number; `feedthrough-layers`: layers written with real shapes |
| `buffer-ports` | Value of `harden` | `true` buffers every input and output port after pin placement |
| `blocks` | — | Hardened blocks: `name` (module), `pnr` (a `harden: true` run), `pnr-path` (default this file). Needs a single-corner platform. See [Assemble hardened blocks](../concepts/pnr.md#assemble-hardened-blocks) |
| `floorplan.utilization` | 0.55 | Fraction of the core area the cells fill |
| `floorplan.aspect` | 1.0 | Die aspect ratio |
| `floorplan.core-margin` | 2.0 | Core-to-die margin in µm |
| `floorplan.die-area`, `floorplan.core-area` | — | `[x0, y0, x1, y1]` in µm, set together, core inside die. Exclude the three keys above |
| `floorplan.core-cutouts` | — | Rectangles removed from the core |
| `floorplan.blockages` | — | Placement blockages; see below |
| `floorplan.macro-placement` | `pack` | `pack` (rb's packer) or `rtl-mp` (OpenROAD's RTL-MP) |
| `floorplan.macro-anchor` | `lower-left` | Core corner the packer starts from. Another corner is an error with `rtl-mp` or a `macro-placement` hook |
| `floorplan.macros`, `floorplan.macros-file` | — | Per-macro directives, inline or from a YAML or JSON file; see below |
| `floorplan.macro-snap` | `true` | `false` keeps macro locations off the site and row grid |
| `floorplan.pins`, `floorplan.pins-file` | — | Pin constraints, inline or from a YAML or JSON file; see below |
| `floorplan.pin-snap` | `true` | `false` keeps exact pin centres instead of moving them to the die edge and a track |
| `floorplan.pins-complete` | `false` | `true` fails the run when a signal port has no entry, more than one exact pin, or no exact pin and several side or group rules |
| `reglvl` | 0 | Regression level: an integer, or a map of tool name to level with an optional `default` |
| `xfail`, `xfail_strict`, `xfail_match` | — | [Expected failures](../concepts/expected-failures.md) |
| `tool_overrides` | — | Accepted, unused |

Each `floorplan.blockages` entry:

- `rect: [x0, y0, x1, y1]` in die µm.
- `type`: `hard` (default; rows cut, macros kept out), `soft` (global placement only) or `partial` with `max-density` between 0 and 1.
- `lifetime`: `flow` (default) or `global-placement`, which exists only during global placement.

Blockages need OpenROAD 26Q1.

Each `floorplan.macros` entry has `instance` (a name, or a glob where `*` crosses `/`) and at least one of:

- `location: [x, y]`: lower-left corner after orientation, die µm. Must match one macro.
- `orientation`: `R0`, `R90`, `R180`, `R270`, `MX`, `MY`, `MXR90` or `MYR90`. Under `rtl-mp` it needs a `location`.
- `halo: [x, y]`: standard-cell keep-out in µm, replacing `placement.macro-cell-halo`.

Only one entry may set each key for a macro.

Each `floorplan.pins` entry has `names` (a name or glob, or a list) and one of:

- `side`: `left`, `right`, `top` or `bottom`, with optional `start` and `end` in die µm along that edge.
- `group: true`, with optional `order: true`, alone or with a `side`.
- `location: [x, y]`: the centre of one pin in die µm, with optional `layer` and `size: [width, height]`.

A `pins-file` entry is a `floorplan.pins` entry or an exact pin with `name`, `location`, optional `layer` and `size`. File entries apply after the inline ones.

`pdn`:

- `ring` (optional): `layers: [horizontal, vertical]`, `width`, `spacing`, `offset` in µm. It must fit between core and die.
- `stripes`: each has `layer` and `width`, plus `followpins: true` or a `pitch` with optional `offset` and `spacing`.
- `connect` (optional): `[lower, upper]` layer pairs. Default: each stripe layer to the next.

It replaces the core grid of the pdn-config in effect, which must exist.

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
| `phys-run` | Optional, synth source only | Synthesis run in `synth-path` whose artefact directory receives this run's `phys-model.json`. A run name, never a path |
| `constraints` | Required for synth source | SDC path. For P&R source it defaults to the routed SDC, the primary corner's on a multi-corner platform |
| `platform` | Required | `cfg-pnr-platforms` entry |
| `lib-paths` | Optional | Extra macro Liberty files relative to `power.yaml`, appended after what the referenced run declares |
| `threads` | Default unset (1) | OpenROAD worker threads, as in `pnr.yaml`. See [OpenROAD threads](../concepts/pnr.md#openroad-threads) |
| `activity.saif` / `.vcd` | Mutually exclusive | Activity trace path |
| `activity.scope` | Only with a trace | OpenROAD trace scope; invalid without SAIF or VCD |
| `activity.default-toggle-rate` | Default 0.1 | Synthetic toggle rate for dynamic mode without a trace |
| `activity.default-static-prob` | Default 0.5 | Synthetic static probability |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Accepted, unused | Reserved per-tool mapping |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |
| `xfail_match` | Optional regex | The failure an `xfail`/`xfail_strict` marker expects; a `FAIL` that does not match stays `FAIL`. Requires a marker |

A P&R source reads the routed ODB and estimates parasitics from global routing. A synthesis source reads the generated netlist. Without `phys-run`, the physical model is written into this run's own `artefacts/<name>/`, so it merges with a synthesis run's half only when both write there.

Hard-macro Liberty is inherited from the run this one reads: a `pnr` source takes the P&R entry's `lib-paths`, and a `synth` source takes the synthesis entry's `lib-paths` and `lef-paths`. `lib-paths` here adds to that list. A configured file that is not on disk fails the run before OpenROAD starts. See [Power Analysis](../concepts/power.md), [Pair the model with a synthesis run](../concepts/power.md#pair-the-model-with-a-synthesis-run), and [Give hard macros a library](../concepts/power.md#give-hard-macros-a-library).

## power_regression.yaml

Required keys are `rtl-buddy-filetype: power_reg_config` and `power-configs`:

```yaml
rtl-buddy-filetype: power_reg_config
power-configs: [power/demo/power.yaml]
```

Paths resolve from the manifest. Each suite keeps the command root of its `power.yaml`. `rb power-regression` filters entries by `--reg-level`.

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
| `tool` | Default `vivado` | Registered backend: `vivado` or `openxc7`. Unknown values are fatal |
| `xdc` | Default empty | Run-specific constraint paths relative to `fpga.yaml` |
| `reglvl` | Default 0 | Regression level |
| `tool_overrides` | Optional map | Backend-specific overrides keyed by tool name |
| `require-timing-met` | Default false | Fails a passing routed run when the backend explicitly reports timing unmet. No effect when timing status is unavailable |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |
| `xfail_match` | Optional regex | The failure an `xfail`/`xfail_strict` marker expects; a `FAIL` that does not match stays `FAIL`. Requires a marker |

Setting both `part` and `platform`, or neither, is fatal. A platform requires `root_config.yaml`. Its XDC files are read first and the run's files afterward.

For `openxc7`, `tool_overrides.openxc7` accepts `chipdb`, `prjxray_db`, `yosys`, `nextpnr`, `fasm2frames`, and `xc7frames2bit`. The `CHIPDB` and `PRJXRAY_DB_DIR` environment variables provide database fallbacks. The openXC7 backend accepts only Xilinx 7-series parts. See [FPGA Implementation](../concepts/fpga.md) for setup, commands, and result metrics.

## fpga_regression.yaml

Required keys are `rtl-buddy-filetype: fpga_reg_config` and `fpga-configs`:

```yaml
rtl-buddy-filetype: fpga_reg_config
fpga-configs: [fpga/counter/fpga.yaml]
```

Paths resolve from the manifest. Each suite keeps the command root of its `fpga.yaml`. `rb fpga-regression` filters entries by `--reg-level`. Discovery checks `./fpga_regression.yaml` before `cfg-rtl-reg.fpga-reg-cfg-path`.

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
| `frontend` | Optional | Analyzer frontend, forwarded as given |
| `single_unit` | Default false | Forwards `--single-unit` for one preprocessor compilation unit |
| `blackbox` | Optional list | Module names forwarded with `--blackbox` |
| `recognized-syncs` | Optional list | Instance regular expressions accepted as synchronizers |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Optional map | Per-analyzer overrides |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |
| `xfail_match` | Optional regex | The failure an `xfail`/`xfail_strict` marker expects; a `FAIL` that does not match stays `FAIL`. Requires a marker |

`rb cdc` produces text and JSON analyzer outputs. See the [CLI reference](cli.md) for commands and options.

## cdc_regression.yaml

Required keys are `rtl-buddy-filetype: cdc_reg_config` and `cdc-configs`:

```yaml
rtl-buddy-filetype: cdc_reg_config
cdc-configs: [lint/cdc/demo/cdc.yaml]
```

Paths resolve from the manifest. Each suite keeps the command root of its `cdc.yaml`. `rb cdc-regression` filters analyses by `--reg-level`. Discovery checks `./cdc_regression.yaml` before `cfg-rtl-reg.cdc-reg-cfg-path`.

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
| `extra_args` | Optional list | Appended after `cfg-verible.extra_args.lint`. Later duplicate flags win |
| `reglvl` | Optional | Regression level |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |
| `xfail_match` | Optional regex | The failure an `xfail`/`xfail_strict` marker expects; a `FAIL` that does not match stays `FAIL`. Requires a marker |

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
| `top` | Default model | Elaboration top. Wins over the model's `top` and follows the [same rule](#model-top) |
| `properties` | Optional | Property files relative to `fpv.yaml`. May be omitted for in-RTL FORMAL properties |
| `constraints` | Optional | One environment-assumption file, read before properties |
| `mode` | Default `bmc` | `bmc`, `prove`, `cover`, or `live` |
| `depth` | Default 20 | Proof depth |
| `engines` | Default `[smtbmc yices]` | SymbiYosys engine specifications |
| `params` | Optional map | Top-level parameter overrides, applied to proof, vacuity, and COI elaboration |
| `reglvl` | Optional | Regression level |
| `covers` | Optional list | Specification coverage IDs; no proof effect |
| `tool_overrides` | Optional map | Per-tool `timeout` and `extra_args` |
| `vacuity` | Default true for bmc/prove, false for cover/live | Derives antecedent reachability covers |
| `vacuity_engines` | Default: the `smtbmc` entries of `engines`, else `[smtbmc yices]` | SymbiYosys engines for the vacuity cover pass. Cover mode needs a BMC engine, so `abc pdr` cannot run it |
| `coi` | Default true | Runs cone-of-influence and dead-assume analysis |
| `frontend` | Default `verilog` | `verilog` or `slang`. Slang requires the configured plugin |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |
| `xfail_match` | Optional regex | The failure an `xfail`/`xfail_strict` marker expects; a `FAIL` that does not match stays `FAIL`. Requires a marker |

Design sources, constraints, and properties are read in that order.

Parameter names must be identifiers. Values are integers, booleans, or strings of whitespace-free SystemVerilog literal text. A string parameter needs embedded quotes, for example `MODE: '"small"'`. Boolean-like keys such as an unquoted `on`, and invalid values, are rejected. The verilog frontend applies parameters with `chparam`, and slang applies `-G` during elaboration.

An `fpv.yaml` `top` and a `mut.yaml` `top` are checked at load time by the same rule as a model `top`. See [Formal Property Verification](../concepts/fpv.md) for frontend behavior, proof-quality checks, artefacts, and counterexamples.

## fpv_regression.yaml

Required keys are `rtl-buddy-filetype: fpv_reg_config` and `fpv-configs`:

```yaml
rtl-buddy-filetype: fpv_reg_config
fpv-configs: [design/example_block/fpv/fpv.yaml]
```

Paths resolve from the manifest. Each suite keeps the command root of its `fpv.yaml`. `rb fpv-regression` filters entries by `--reg-level`.

## release.yaml

Required keys are `rtl-buddy-filetype: release_config`, `name`, `version`, `design` and `encryption`. Unknown keys are errors, so a misspelt protection setting cannot ship a file unprotected. Paths resolve relative to `release.yaml`. See [Customer releases](../concepts/release.md).

```yaml
rtl-buddy-filetype: release_config
name: acme
version: 1.0.0
metadata: {cut: acme_cut}

design:
  model: acme_top
  model-config: ../../rtl/models.yaml
  top: acme_top
  protect: {obfuscate: true, encrypt: true, strip-comments: true}
  preserve:
    interfaces: [acme_core_wrap]
    identifiers: [ACME_SIM]
  files:
    - {match: "acme_pub_pkg.sv", encrypt: false, reason: "constants the customer programs"}
  externals:
    - {path: "$VENDOR_LIB", ship-as: "$VENDOR_LIB"}
  constraints:
    - {src: ../../rtl/acme_top.sdc, scope: acme_top}

testbench:
  filelist: [ "tb/tb_acme.sv" ]
  extra-files: [ "tb/run.sh" ]
  allow-design-refs: [acme_pub_pkg]

verify:
  command: "bash verif/run.sh"
  pass: "^TEST PASSED"
  compare: "^SIG: .*"

encryption:
  key-file: keys/vendor_keys.txt

csr:
  gate: acme_customer
  windows:
    - {name: main, rdl: ../../rtl/csr/acme_csr.rdl, top: acme_csr, base: 0x40000000}
  registers:
    - {match: "main.ctrl", obfuscate-fields: ["dbg_*"]}
    - {match: "main.chan.*"}

package:
  notes: "notes/{version}.md"
  docs: [ docs/user_guide.md ]
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Release name; the tarball is `<name>-<version>.tar.gz` |
| `version` | Required | Release version; names the archived map `maps/<version>.map`. Versions order numerically by part |
| `metadata` | Optional map | Copied into the internal manifest |
| `design.model` / `design.model-config` | Required | The model whose filelist is released, and its `models.yaml` |
| `design.top` | Default model top | Top module; its interface is always preserved |
| `design.protect` | Default all true | `obfuscate`, `encrypt` and `strip-comments` defaults for design files |
| `design.preserve.interfaces` | Optional list | Modules whose name, ports and parameters are kept: hardening boundaries and constraint scopes |
| `design.preserve.identifiers` | Optional list | Further names kept everywhere |
| `design.files` | Optional list | Per-file overrides: `match` (a glob on the base name, or on the project-relative path when it contains `/`), `reason` (required), and any of `obfuscate`, `encrypt`, `strip-comments`, `dir` (the package directory the file ships in). Later rules win; a rule matching nothing is an error |
| `design.externals` | Optional list | `path` (environment variables expanded; relative to `release.yaml` unless absolute) whose files are not shipped, `ship-as`, the prefix written in their place, and optional `dir`, the package directory whose filelist carries the references (default `design`) |
| `design.constraints` | Optional list | `src` SDC file, `scope` (a preserved module), optional `ship-as` file name, and `mode`: `rewrite` (default; evaluate and translate) or `verbatim` (ship unchanged after a port check) |
| `testbench.filelist` | Optional list | Filelist lines, as in `models.yaml`, relative to `release.yaml` |
| `testbench.extra-files` | Optional list | Files copied into `verif/` unchanged, such as run scripts |
| `testbench.protect` / `testbench.files` | Default not protected | As for `design`; testbench obfuscation is not supported |
| `testbench.allow-design-refs` | Optional list | Design units the testbench may name, which therefore ship unobfuscated |
| `verify.command` | Required in `verify` | Shell command run from the root of each stage |
| `verify.pass` | Required in `verify` | Regex the output must match, with exit status 0 |
| `verify.compare` | Optional | Regex whose matching lines must be identical in every stage |
| `verify.stages` | Default `[src, obf, pkg]` | Stages to run |
| `verify.timeout` | Default 3600 | Seconds per stage |
| `encryption.key-file` | Required | IEEE-1735 key file for `vcs -ipprotect` |
| `encryption.vcs` | Default `vcs` | VCS executable |
| `encryption.extra-args` | Optional list | Extra `vcs` arguments |
| `encryption.jobs` | Default 4 | Files encrypted in parallel |
| `obfuscation.verible` | Default `cfg-verible`, then `PATH` | Path to `verible-verilog-obfuscate` |
| `obfuscation.map-dir` | Default `maps` | Where maps and manifests are archived |
| `obfuscation.continue-from` | Default `previous` | `previous`, `none`, or a map file to start from |
| `obfuscation.keep-comments` | Default directive patterns | Regexes; a matching comment survives stripping |
| `obfuscation.token-paste` | Default `refuse` | `refuse` fails on token pasting that forms names; `preserve` keeps every name it can form |
| `csr.windows` | Required in `csr` | Address maps that ship registers: `name` (an identifier, used in `match` and macro names), `rdl` source, `top` address map and absolute `base` |
| `csr.registers` | Required in `csr` | The whitelist: `match`, a glob on `<window>.<path>` (instance names, no array indices) naming registers, regfiles or memories, and optional `obfuscate-fields`, field-name globs shipped as `f<lsb>` |
| `csr.gate` | Optional | User-defined property that makes a register, regfile or memory eligible; without it every register is |
| `csr.name` / `csr.prefix` | Default `<name>_csr` / its upper case | Base name of the `csr/` files, and the SystemVerilog macro prefix |
| `csr.include-dirs` | Optional list | RDL `` `include `` search paths |
| `package.notes` | Default `notes/{version}.md` | Release notes, required for every release; shipped as `RELEASE_NOTES.md` |
| `package.docs` | Optional list | Documents shipped in `docs/` |

## mut.yaml

Required keys are `rtl-buddy-filetype: mut_config`, `model`, `model_path`, `design_file`, `verify`, and at least one entry in `operators` or `mutants`.

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
| `operators` | Default empty | `arith_flip`, `bit_op_flip`, `cond_negate`, `cond_const`, `assign_drop`, `port_binding_swap` |
| `mutants[].name` | Required per entry | Unique user-mutant name: letters, digits, `_`, `.`, `-`; mutant id `user_<name>` |
| `mutants[].patch` | Required per entry | Unified diff relative to `mut.yaml`; its paths are relative to the model directory |
| `mutants[].strip` | Default 1 | Leading path components removed from the diff's paths, as `git apply -p` |
| `verify.fpv_config` / `.verification` | Pair | FPV oracle config and entry |
| `verify.test_config` | Optional | Simulation oracle suite |
| `verify.tests` | Default all | Selected simulation tests |
| `verify.assertions` | Default true | Enables Verilator assertions for the simulation oracle |
| `name` | Default model | Campaign and artefact name |
| `top` | Default model | Top module the FPV oracle elaborates. Follows the [same rule](#model-top) as a model `top` |
| `budget.max_mutants` | Default 100 | Global campaign cap |
| `budget.per_file_cap` | Default null | Per-scoped-file cap |
| `budget.time_budget_minutes` | Default null | Wall-clock cap |
| `budget.schedule` | Default `sequential` | `sequential` or `round_robin` |
| `scope.include` / `.exclude` | Default empty | Case-sensitive `fnmatch` globs over instance and source paths; `**` is not recursive |

At least one oracle is required, and `fpv_config` requires `verification`. `design_file` and every scoped file must stay within the model directory.

- A user mutant whose diff no longer applies is `ERRORED`. The budget does not limit user mutants, and `scope` requires a non-empty `operators`. See [Add curated mutants](../concepts/mut.md#add-curated-mutants).

- An empty scope mutates `design_file` without the viewer.
- A non-empty scope requires `rtl-buddy-view`, selects hierarchy source files, and fails if none match.
- A campaign's `top` wins over the model's and the oracle verification's. It applies to the baseline proof and every mutant proof, since their verdicts are comparable only when elaborated from the same root.
- Only the FPV oracle elaborates a top. A campaign that configures only the simulation oracle logs `mut_config.top_override_unused` and ignores `top`.

See [Mutation Testing](../concepts/mut.md).

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
| `blocks[].name` | Required | Block identifier, matched to model name in multi-block specs |
| `blocks[].desc` | Required | Human-readable description |
| `blocks[].docs` | Optional list | Markdown paths relative to `specs.yaml` |
| `blocks[].coverage-items` | Default empty | Functional coverage item list |
| `coverage-items[].id` | Required | Identifier used by `covers` in tests and formal verifications |
| `coverage-items[].desc` | Required | Verification requirement |

A single-block file matches its linked model unconditionally. These fields affect traceability only. See [Spec Traceability](../concepts/spec-traceability.md).
