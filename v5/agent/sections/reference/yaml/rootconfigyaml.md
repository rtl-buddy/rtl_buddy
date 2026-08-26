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
- `cfg-surfer` configures the Surfer waveform viewer used by `rb wave`. `path` is a bare executable name (resolved via PATH) or a relative/absolute path to the binary. `editor-cmd` supports `%f` (file path) and `%l` (line number) placeholders. `editor-terminal` controls how the editor is launched: `tmux` opens a new tmux window, `iterm2` and `terminal` use AppleScript, empty string runs the command directly (suitable for GUI editors like VS Code). `editor-sock` is an optional Unix socket path that enables nvim remote reuse: rtl-buddy launches nvim with `--listen <sock>` on first use and reconnects for subsequent events. `ctrl-sock` is an optional Unix socket for the wave control server, which lets nvim send signals to Surfer — press `<Space>wa` (or your `<leader>wa`) on a signal name to add it to the waveform view. Install the bundled nvim plugin first with `rb wave-install-nvim`.
- `cfg-synth-tools` defines synthesis tool entries selected by `synth.yaml` `tool` fields. `tool` is the path to the executable, or a bare name if it is available on `PATH`. For the Yosys backend, `opts.synth-args` are appended to the `synth` command and `opts.abc-args` are used by the unmapped ABC step. For the OpenROAD backend, `opts.strategy` controls optional resynthesis (`AREA` = none, `TIMING`/`TIMING_ANNEAL` = `resynth_annealing`, `TIMING_GENETIC` = `resynth_genetic`). `opts.frontend` selects the SystemVerilog parser: `"verilog"` (default) uses Yosys's built-in `read_verilog -sv -defer` per source — fast, lazy elaboration, but a small SV subset. `"slang"` loads the [yosys-slang](https://github.com/povik/yosys-slang) plugin and calls `read_slang` instead — full SV-2017 (package imports, packed-struct typedefs, complex generates) with eager elaboration. `opts.plugin-path` is required when `frontend: slang`; absolute paths pass through and relative paths resolve against the project root. Both options accept per-block overrides via `synth.yaml` `tool_overrides.yosys.frontend` / `.plugin_path` (note: `tool_overrides` keys are snake_case Python attribute names, while `cfg-synth-tools.opts` uses kebab-case YAML — same field, two names, see [synthesis concept doc](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/synthesis/#systemverilog-frontend) for the convention). The override key is always `yosys` (the elaboration tool), regardless of whether the synth selects `tool: yosys` or `tool: openroad`. The OpenROAD backend runs Yosys for elaboration → write_verilog → OpenROAD reads the netlist, so its elaboration-stage opts come from the `yosys` tool config + `tool_overrides.yosys` block.
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
