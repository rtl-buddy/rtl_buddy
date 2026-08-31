---
description: Configure and run Yosys synthesis with optional OpenROAD timing analysis, PDK mapping, slang parsing, and effort levels.
---

# Synthesis

`rb synth` reads one or more runs from `synth.yaml`, resolves RTL through a model file, and writes a netlist and reports under the config directory.

## Choose a backend

| `tool:` | Flow | Clock handling | Results |
| --- | --- | --- | --- |
| `yosys` | Yosys and ABC | Uses the minimum SDC clock period | Gates, area, WNS |
| `openroad` | Yosys mapping, then OpenROAD STA | Reads the full multi-clock SDC | Gates, area, WNS, TNS |

Use `yosys` for technology-independent synthesis or a quick mapped result. Use `openroad` when timing must respect multiple clocks or you need OpenROAD STA.

Both backends use Yosys for RTL elaboration and mapping. The OpenROAD backend adds a second stage over the mapped netlist.

## Install the tools

RTL Buddy validates against the [RTL Buddy Yosys fork](https://github.com/rtl-buddy/yosys):

```bash
git clone --recursive https://github.com/rtl-buddy/yosys.git
cd yosys
make config-clang
make -j 8
make install
yosys --version
```

Use `make config-gcc` on Linux when appropriate. Ensure `yosys` is on `PATH`.

For `tool: openroad`, build OpenROAD and put `openroad` on `PATH`:

```bash
openroad -version
```

On macOS, use the source-build instructions in the project template's `tools/openroad/SETUP_OSX.md`.

## Define synthesis runs

A minimal `synth.yaml` can contain an unmapped and a technology-mapped run:

```yaml
rtl-buddy-filetype: synth_config

syntheses:
  - name: sandbox_rtl
    desc: Technology-independent synthesis
    model: test_module
    model_path: ../../design/sandbox/models.yaml
    tool: yosys
    reglvl: 0

  - name: sandbox_openroad
    desc: SKY130 mapping and timing
    model: test_module
    model_path: ../../design/sandbox/models.yaml
    tool: openroad
    platform: sky130hd_tt
    constraints: constraints.sdc
    params:
      WIDTH: 8
    defines:
      TARGET_SYNTH: 1
    reglvl: 0
```

Paths resolve from `synth.yaml`. The synthesis top is the model's root module — its `top:` in `models.yaml`, defaulting to the model name. `platform` enables Liberty mapping; the OpenROAD backend additionally requires LEF assets.

Use `lef-paths` and `lib-paths` for block-specific hard macros. Use `tool_overrides` only for backend options that have no portable equivalent. See [YAML Formats: synth.yaml](../reference/yaml.md#synthyaml) for all fields.

## Configure tools and the PDK

Define backend defaults and map a named synthesis platform to a PDK corner in `root_config.yaml`:

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      synth-args: ""
      abc-args: ""
      frontend: verilog

  - name: openroad
    tool: openroad
    opts:
      strategy: AREA
      frontend: verilog

cfg-pdks:
  - name: sky130hd
    site: unithd
    corners:
      tt: pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
    tech-lef: pdk/sky130hd/lef/sky130_fd_sc_hd.tlef
    macro-lef: pdk/sky130hd/lef/sky130_fd_sc_hd_merged.lef

cfg-synth-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    corner: tt
```

All paths resolve from `root_config.yaml`.

The Yosys backend uses Liberty for mapping, area, and timing. The OpenROAD backend requires Liberty and technology/macro LEF; a missing LEF fails before running the tool. Keep large PDK files untracked and provide a reproducible fetch script.

OpenROAD `strategy` values are `AREA`, `TIMING`, `TIMING_ANNEAL`, and `TIMING_GENETIC`. `AREA` reports the initial mapping; the timing strategies request OpenROAD resynthesis.

## Use SDC constraints

A Yosys run extracts `create_clock` periods from the SDC and supplies the shortest period to ABC. It warns when multiple clocks require this approximation.

An OpenROAD run loads the complete SDC and reports actual worst and total negative slack. Use it for multi-clock timing decisions.

## SystemVerilog frontend

Use yosys-slang when the built-in `read_verilog -sv` frontend cannot parse the design:

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      frontend: slang
      plugin-path: ../yosys-slang/build/slang.so
      single-unit: false
```

Build the plugin against the same Yosys installation. `plugin-path` resolves from the project root. If omitted, RTL Buddy checks `RTL_BUDDY_SLANG_PLUGIN`; that environment value must be absolute, although `~` is expanded.

Set `single-unit: true` only when source files intentionally share preprocessor definitions across file boundaries. It applies only to slang; with the Verilog frontend it is ignored with a warning. Non-Boolean values are fatal.

For one run, override the Yosys elaboration stage:

```yaml
tool_overrides:
  yosys:
    frontend: slang
    plugin_path: ../yosys-slang/build/slang.so
    single_unit: true
```

Under `cfg-synth-tools.opts`, fields use kebab case such as `plugin-path` and `single-unit`. Under `tool_overrides.yosys`, use snake case such as `plugin_path` and `single_unit`. Unknown override keys are warned about and ignored.

The override key remains `yosys` even when the run's backend is `openroad`, because Yosys owns elaboration.

## Select an effort

Define reusable effort levels in `root_config.yaml`:

```yaml
cfg-synth-efforts:
  - name: quick
    yosys:
      synth-args: -flatten
      abc-args: -fast
    openroad:
      run: false

  - name: standard
    openroad:
      run: true

  - name: accurate
    openroad:
      run: true
      pre-sta-tcl: |
        set_wire_load_mode top
        set_wire_load_model -name Small
```

Select an effort in the run or on the CLI:

```yaml
effort: quick
```

```bash
rb synth sandbox_openroad --effort quick
rb synth-regression --effort accurate
```

Precedence is per-run `tool_overrides`, then the selected effort, then `cfg-synth-tools`. Without a configured or selected effort, RTL Buddy uses built-in `standard` behavior.

`openroad.run: false` skips OpenROAD and returns the Yosys result. `pre-sta-tcl` is raw Tcl executed before STA; test it on a small design because syntax and tool errors appear only at runtime.

## Synthesize hard macros

For each hard macro:

1. Add its physical LEF to `lef-paths`.
2. Add its timing Liberty to `lib-paths`.
3. Provide a port-only RTL `(* blackbox *)` declaration for frontend binding.

The OpenROAD stage avoids generating a Verilog stub when the macro already exists in the supplied LEF or Liberty, preserving its physical area and timing arcs. If no physical or timing master exists, RTL Buddy generates a port-only stub and the reported PPA cannot represent that macro accurately.

## Run synthesis

```bash
rb synth --list -c synth/block/synth.yaml
rb synth block_openroad -c synth/block/synth.yaml
rb synth -c synth/block/synth.yaml
rb synth-regression -c synth_regression.yaml
rb synth-regression -c synth_regression.yaml --reg-level 1000
```

A synthesis regression manifest lists config files relative to itself:

```yaml
rtl-buddy-filetype: synth_reg_config
synth-configs:
  - synth/block_a/synth.yaml
  - synth/block_b/synth.yaml
```

## Interpret results

Mapped runs report gates and area. Constrained Yosys runs report WNS as clock period minus critical-path delay. OpenROAD reports actual WNS and TNS; negative values indicate violations and TNS 0 indicates no negative endpoint slack.

A Yosys run passes when the process exits 0 and its log has no `ERROR:` line. An OpenROAD run requires both the Yosys and OpenROAD stages to exit 0 and rejects OpenROAD `[ERROR ...]` lines. Any failed stage reports `FAIL`.

## Inspect artefacts

Outputs land under `<synth-dir>/artefacts/<run>/`.

| File | Backend | Purpose |
| --- | --- | --- |
| `synth.f`, `synth.ys` | Both | Resolved sources and generated Yosys script |
| `synth.rtlil` | Unmapped Yosys | Technology-independent netlist |
| `synth_netlist.v` | Mapped runs | Gate-level Verilog |
| `synth.log` | Yosys-only | Yosys output |
| `synth_yosys.log` | OpenROAD | First-stage Yosys output |
| `synth.tcl`, `synth.log` | OpenROAD | STA script and OpenROAD output |

Both netlists are deleted at the very start of each run, before the filelist is even generated and before Yosys is looked for at all, so every way a run can fail leaves them absent — there is no missing-tool carve-out here, because `rb pnr` and `rb power` resolve the netlist by path and must never be handed the previous run's. A run that fails publishes nothing. Yosys writes the netlist partway through its script and only then runs the trailing `stat`, so it can crash — or log an `ERROR:` line — with the netlist already on disk; and on the OpenROAD backend the Yosys stage can succeed before the timing stage fails. Every one of those paths removes the netlist again, so a `FAIL` never leaves a design for `rb pnr` or `rb power` to pick up. They are the fixed-path inputs `rb pnr` and `rb power` resolve, so a failed rerun that left the last successful run's netlist in place would have those commands place, route, and power-analyse a design that is no longer what the RTL says. A failed run therefore leaves no netlist at all, and `rb pnr` reports that you need to run `rb synth` first. Copy a netlist you want to compare against out of the artefact directory before rerunning.

When a run fails, inspect the relevant stage log first. Missing tools, plugin paths, Liberty, or LEF inputs are configuration failures; correct the path or installation and rerun the named synthesis.
