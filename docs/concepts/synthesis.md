---
description: How to run synthesis flows with rtl_buddy using synth.yaml, cfg-synth-tools, cfg-synth-libs, and the rb synth command.
---

# Synthesis

`rtl_buddy` provides a tool-agnostic synthesis flow that mirrors the simulation workflow. Synthesis runs are described in `synth.yaml` files; tool-specific defaults and PDK library paths live in `root_config.yaml`.

## Supported backends

`rtl_buddy` ships two synthesis backends selectable via `tool:` in `synth.yaml`:

| `tool:` | Backend | Multi-clock SDC | Reports |
|---------|---------|-----------------|---------|
| `yosys` | Yosys + ABC | Workaround (min period) | Gates, Area, WNS |
| `openroad` | Yosys (stage 1) + OpenROAD STA (stage 2) | Native `read_sdc` | Gates, Area, WNS, TNS |

The OpenROAD backend removes the multi-clock SDC workaround: stage 1 maps RTL to a gate-level netlist with Yosys, stage 2 feeds that netlist into OpenROAD which loads the SDC natively and reports WNS (actual worst slack from `report_checks`) and TNS (total negative slack).

## Installing Yosys

`rtl_buddy` uses the [rtl-buddy fork of Yosys](https://github.com/rtl-buddy/yosys), which tracks upstream with rtl-buddy-specific patches. Build from source:

```bash
# Install deps with brew. Adjust to your package manager as needed.
brew install cmake python tcl-tk libffi readline

# Clone, build and install
git clone --recursive https://github.com/rtl-buddy/yosys.git
cd yosys
make config-clang   # or `make config-gcc` on Linux
make -j 8           # adjust to no. of CPU cores if needed
make install        # installs to /usr/local/bin/yosys
```

Verify the install:

```bash
yosys --version
```

The `yosys` binary must be on `PATH` when `rb synth` is invoked.

## Installing OpenROAD

OpenROAD is required only for the `openroad` backend. It must be built from source on macOS — no official binaries are published. See the build notes in your project's `tools/openroad/BUILD_OSX.md` for the full procedure. After building, symlink the binary to a directory on `PATH`:

```bash
ln -s /path/to/OpenROAD/build/bin/openroad /usr/local/bin/openroad
openroad -version
```

The `openroad` binary must be on `PATH` when `rb synth` is invoked with `tool: "openroad"`.

## Synthesis config: `synth.yaml`

A `synth.yaml` file defines one or more synthesis runs for a block.

```yaml
rtl-buddy-filetype: synth_config

syntheses:
  # Technology-independent run
  - name: "sandbox_synth"
    desc: "Synthesize sandbox module with Yosys"
    model: "test_module"
    model_path: "../../design/sandbox/models.yaml"
    tool: "yosys"
    reglvl: 0

  # Technology-mapped run targeting SKY130 (Yosys backend)
  - name: "sandbox_sky130"
    desc: "Synthesize sandbox module targeting SKY130 HD TT corner"
    model: "test_module"
    model_path: "../../design/sandbox/models.yaml"
    tool: "yosys"
    libraries:
      - "sky130hd_tt"
    constraints: "constraints.sdc"
    params:
      WIDTH: 8
    defines:
      TARGET_SYNTH: 1
    reglvl: 0
    tool_overrides:
      yosys:
        synth_args: "-flatten"

  # Technology-mapped run with OpenROAD backend (native multi-clock SDC, WNS + TNS)
  - name: "sandbox_openroad"
    desc: "Synthesize sandbox module with OpenROAD timing analysis"
    model: "test_module"
    model_path: "../../design/sandbox/models.yaml"
    tool: "openroad"
    libraries:
      - "sky130hd_tt"
    constraints: "constraints.sdc"
    reglvl: 0
```

### Synthesis fields

| Field | Description |
|-------|-------------|
| `name` | Run identifier used on the command line and in artefact paths |
| `desc` | Human-readable description |
| `model` | Model name from `models.yaml`; also used as the synthesis top module |
| `model_path` | Path to `models.yaml`, resolved relative to the `synth.yaml` directory |
| `tool` | Synthesis tool name — must match a `cfg-synth-tools` entry in `root_config.yaml` |
| `libraries` | Optional list of library names from `cfg-synth-libs`; enables technology mapping |
| `constraints` | Optional SDC constraints file, resolved relative to `synth.yaml` |
| `params` | Optional key-value pairs passed as top-level parameter overrides (`chparam` in Yosys) |
| `defines` | Optional compile-time Verilog defines passed via `-D KEY=VALUE` |
| `reglvl` | Regression level (int or per-tool dict); same semantics as simulation `reglvl` |
| `tool_overrides` | Optional per-tool option overrides — keyed by tool name, merges over `cfg-synth-tools` defaults |

### SDC constraints

When `constraints` points to an SDC file, the Yosys backend extracts the `create_clock` period and passes it to ABC as `-D <period_ps>` for timing-driven technology mapping. The critical path delay is then used to compute WNS in the results table.

```sdc
create_clock -period 10.0 [get_ports clk]
set_input_delay  2.0 -clock clk [all_inputs]
set_output_delay 2.0 -clock clk [all_outputs]
```

**Multi-clock designs (Yosys backend):** ABC's `-D` flag takes a single timing window. When multiple `create_clock` entries are present, `rtl_buddy` uses the minimum period as a workaround and emits a warning. For correct per-domain timing analysis across multiple clocks, use the `openroad` backend, which passes the full SDC to `read_sdc` and handles each clock domain natively.

### Regression levels

`reglvl` works the same way as for simulation tests. Use `--reg-level` on `synth-regression` to filter by level.

```yaml
# Same level for all tools
reglvl: 0

# Tool-specific with fallback
reglvl:
  default: 0
  dc: 1000
```

### Per-tool overrides

`tool_overrides` is an escape hatch for tool-specific options that don't have a tool-agnostic equivalent. Keys match the `opts` fields in `cfg-synth-tools`:

```yaml
tool_overrides:
  yosys:
    synth_args: "-flatten -nordff"
    abc_args: "-fast"
```

## Root config: `root_config.yaml`

### Synthesis tool configuration

Synthesis tool defaults live under `cfg-synth-tools`. Multiple tools can be listed; the `tool` field in `synth.yaml` selects which entry to use:

```yaml
cfg-synth-tools:
  - name: "yosys"
    tool: "yosys"        # executable name (must be on PATH)
    opts:
      synth-args: ""
      abc-args: ""

  - name: "openroad"
    tool: "openroad"     # executable name (must be on PATH)
    opts:
      strategy: "AREA"   # AREA (default) | TIMING | TIMING_ANNEAL | TIMING_GENETIC
```

The `strategy` option controls optional OpenROAD resynthesis after timing analysis:

| `strategy` | Effect |
|------------|--------|
| `AREA` (default) | No resynthesis; report area and timing only |
| `TIMING` / `TIMING_ANNEAL` | Run `resynth_annealing` after loading the netlist |
| `TIMING_GENETIC` | Run `resynth_genetic` after loading the netlist |

### PDK library configuration

Liberty files for technology mapping are registered under `cfg-synth-libs`. Paths are resolved relative to `root_config.yaml`:

```yaml
cfg-synth-libs:
  - name: "sky130hd_tt"
    path: "pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"
    lef-paths:                                          # required for OpenROAD backend
      - "pdk/sky130hd/lef/sky130_fd_sc_hd_merged.lef"
```

The `libraries` list in `synth.yaml` references entries by name.

- **Yosys backend:** uses `path` (liberty) for `read_liberty` → `dfflibmap` → `abc -liberty` → `write_verilog`. `lef-paths` is ignored.
- **OpenROAD backend:** requires both `path` (liberty) for timing and `lef-paths` (LEF) for technology loading. Without `lef-paths` the run fails immediately with an actionable error.

PDK files are typically large and should be gitignored. Provide a download script:

```bash
# pdk/download_pdk.sh
curl -fL <liberty-url> -o pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
curl -fL <lef-url>     -o pdk/sky130hd/lef/sky130_fd_sc_hd_merged.lef
```

## Synthesis regression: `synth_regression.yaml`

`synth_regression.yaml` lists the `synth.yaml` files to include in a synthesis regression:

```yaml
rtl-buddy-filetype: synth_reg_config

synth-configs:
  - "synth/sandbox/synth.yaml"
  - "synth/dma/synth.yaml"
```

Paths are resolved relative to `synth_regression.yaml`.

## Running synthesis

Run all syntheses in a config:
```bash
rtl-buddy synth -c synth/sandbox/synth.yaml
```

Run a named synthesis:
```bash
rtl-buddy synth sandbox_sky130 -c synth/sandbox/synth.yaml
```

List syntheses without running:
```bash
rtl-buddy synth --list -c synth/sandbox/synth.yaml
```

Run a synthesis regression:
```bash
rtl-buddy synth-regression -c synth_regression.yaml
```

Run only up to regression level 0:
```bash
rtl-buddy synth-regression -c synth_regression.yaml --reg-level 0
```

## Results table

`rb synth` prints a results table after each run. Columns appear conditionally:

```
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Synthesis        ┃ Result ┃ Description      ┃ Gates ┃ Area       ┃ WNS       ┃ TNS       ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━┩
│ sandbox_synth    │ PASS   │ Synthesis passed │ 18    │ -          │ -         │ -         │
│ sandbox_sky130   │ PASS   │ Synthesis passed │ 18    │ 178.92 µm² │ +8.882 ns │ -         │
│ sandbox_openroad │ PASS   │ Synthesis passed │ 18    │ 179.00 µm² │ +6.754 ns │ +0.000 ns │
└──────────────────┴────────┴──────────────────┴───────┴────────────┴───────────┴───────────┘
```

| Column | Source | When shown |
|--------|--------|-----------|
| **Gates** | Yosys `stat` cell count | All lib-mapped flows |
| **Area** | Yosys `stat -liberty` / OpenROAD `report_design_area` | Lib-mapped flows |
| **WNS** | Yosys: clock period − critical path delay; OpenROAD: `report_checks -path_delay max` | Lib-mapped flows with SDC |
| **TNS** | OpenROAD `report_tns` — sum of all negative endpoint slacks | OpenROAD backend with SDC |

WNS and TNS are positive when timing is met and negative when violated. TNS = 0 means no violations; a negative TNS indicates the total repair budget needed.

**WNS difference between backends:** Yosys computes WNS as `period − critical_path`, which always reports positive slack. OpenROAD's `report_checks` reports the actual worst slack across all timing paths. The two values are closely aligned for single-clock designs.

## Artefacts

Synthesis artefacts land under `artefacts/<synth_name>/` relative to the `synth.yaml` directory.

**Yosys backend:**

| File | Contents |
|------|----------|
| `synth.f` | Generated source filelist (resolved from `models.yaml`) |
| `synth.ys` | Generated Yosys script |
| `synth.log` | Captured Yosys stdout and stderr |
| `synth.rtlil` | Output netlist, technology-independent flow (RTLIL format) |
| `synth_netlist.v` | Output netlist, technology-mapped flow (Verilog) |

**OpenROAD backend:**

| File | Contents |
|------|----------|
| `synth.f` | Generated source filelist |
| `synth.ys` | Yosys script (stage 1 — maps RTL to gate-level netlist) |
| `synth_yosys.log` | Yosys stdout and stderr |
| `synth_netlist.v` | Gate-level Verilog produced by Yosys, fed into OpenROAD |
| `synth.tcl` | OpenROAD Tcl script (stage 2 — timing analysis) |
| `synth.log` | OpenROAD stdout and stderr |

## Pass/fail detection

**Yosys backend:** a run passes when the tool exits with code 0 and `synth.log` contains no lines starting with `ERROR:`.

**OpenROAD backend:** both stages must succeed. The Yosys stage applies the same exit-code and `ERROR:` check; the OpenROAD stage checks exit code and the absence of `[ERROR ...]` lines in `synth.log`.

Any other outcome is **FAIL** with a description in the results table.

## Full schema

See [YAML Formats: synth.yaml](../reference/yaml.md#synthyaml) for the complete field reference.
