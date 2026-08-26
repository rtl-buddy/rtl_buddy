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
- `cfg-synth-efforts` defines named synthesis effort levels referenced by `synth.yaml` `effort` fields or the `--effort` CLI flag. Each entry has optional `yosys.synth-args` / `yosys.abc-args` (merged into the Yosys stage) and an `openroad` block. When `openroad.run: false`, the runner falls back to the Yosys-only backend even if `tool: openroad` was selected — useful for a fast quick-look path that needs no LEF/STA. `openroad.pre-sta-tcl` is a raw Tcl snippet injected into `synth.tcl` between `read_sdc` and `report_checks`; use it to insert floorplan/placement/parasitic-estimation steps before timing analysis. When no `cfg-synth-efforts` entries are configured or no effort is selected, a built-in `standard` effort with all defaults is used. Precedence for the same knob: per-synthesis `tool_overrides` > `cfg-synth-efforts` > `cfg-synth-tools`.
- `cfg-cdc-tools` defines CDC tool entries selected by `cdc.yaml` `tool` fields. `tool` is the executable name on `PATH` (or an absolute path). `opts.sync-depth` is forwarded as `--sync-depth N` and controls CDC-002's required synchronizer depth. `opts.extra-args` is appended verbatim to every analyzer invocation.
- `cfg-rtl-reg.reg-cfg-path` is the fallback regression file for `rtl-buddy regression` when no `./regression.yaml` exists in the cwd.
- `cfg-verible[].path` is the directory containing Verible executables. Absolute paths are used as-is; relative paths are resolved from the directory containing `root_config.yaml`.

---
