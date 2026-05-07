---
description: How to run synthesis flows with rtl_buddy using synth.yaml, cfg-synth-tools, cfg-synth-libs, and the rb synth command.
---

# Synthesis

`rtl_buddy` provides a tool-agnostic synthesis flow that mirrors the simulation workflow. Synthesis runs are described in `synth.yaml` files; tool-specific defaults and PDK library paths live in `root_config.yaml`.

## Installing Yosys

`rtl_buddy` uses the [rtl-buddy fork of Yosys](https://github.com/rtl-buddy/yosys), which tracks upstream with rtl-buddy-specific patches. Build from source:

```bash
git clone https://github.com/rtl-buddy/yosys.git
cd yosys
make config-clang   # or config-gcc on Linux
make -j$(nproc)
sudo make install   # installs to /usr/local/bin/yosys
```

On macOS with Homebrew dependencies:

```bash
brew install cmake python tcl-tk libffi readline
git clone https://github.com/rtl-buddy/yosys.git
cd yosys
make config-clang
make -j$(sysctl -n hw.logicalcpu)
sudo make install
```

Verify the install:

```bash
yosys --version
```

The `yosys` binary must be on `PATH` when `rb synth` is invoked.

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

  # Technology-mapped run targeting SKY130
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

**Multi-clock designs:** ABC's `-D` flag takes a single timing window. When multiple `create_clock` entries are present, `rtl_buddy` uses the minimum period as a workaround and emits a warning. For proper multi-clock synthesis, create separate `synth.yaml` entries per clock domain, each with its own SDC.

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

Synthesis tool defaults live under `cfg-synth-tools`:

```yaml
cfg-synth-tools:
  - name: "yosys"
    tool: "yosys"        # executable name (must be on PATH)
    opts:
      synth-args: ""
      abc-args: ""
```

Multiple tools can be listed. The `tool` field in `synth.yaml` selects which entry to use.

### PDK library configuration

Liberty files for technology mapping are registered under `cfg-synth-libs`. Paths are resolved relative to `root_config.yaml`:

```yaml
cfg-synth-libs:
  - name: "sky130hd_tt"
    path: "pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"
  - name: "sky130hd_ss"
    path: "pdk/sky130hd/lib/sky130_fd_sc_hd__ss_100C_1v60.lib"
```

The `libraries` list in `synth.yaml` references entries by name. When libraries are specified, the Yosys backend switches to a technology-mapped flow: `read_liberty` → `synth` → `dfflibmap` → `abc -liberty` → `write_verilog`.

Liberty files are typically large and should be gitignored. Provide a download script in your project:

```bash
# pdk/download_pdk.sh
curl -fL <liberty-url> -o pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
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
┏━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Synthesis      ┃ Result ┃ Description      ┃ Gates ┃ Area       ┃ WNS       ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ sandbox_synth  │ PASS   │ Synthesis passed │ 18    │ -          │ -         │
│ sandbox_sky130 │ PASS   │ Synthesis passed │ 18    │ 178.92 µm² │ +8.882 ns │
└────────────────┴────────┴──────────────────┴───────┴────────────┴───────────┘
```

| Column | Source | When shown |
|--------|--------|-----------|
| **Gates** | `stat` cell count | Always (all Yosys flows) |
| **Area** | `stat -liberty` chip area | Lib-mapped flows only |
| **WNS** | Clock period − critical path (`stime -p`) | Lib-mapped flows with SDC clock constraint |

WNS (Worst Negative Slack) is positive when timing is met and negative when violated.

## Artefacts

Synthesis artefacts land under `artefacts/<synth_name>/` relative to the `synth.yaml` directory:

| File | Contents |
|------|----------|
| `synth.f` | Generated source filelist (resolved from `models.yaml`) |
| `synth.ys` | Generated Yosys script |
| `synth.log` | Captured tool stdout and stderr |
| `synth.rtlil` | Output netlist, technology-independent flow (RTLIL format) |
| `synth_netlist.v` | Output netlist, technology-mapped flow (Verilog) |

## Pass/fail detection

A synthesis run is marked **PASS** when:

1. The tool exits with code 0, **and**
2. No lines starting with `ERROR:` appear in `synth.log`.

Any other outcome is **FAIL** with a description in the results table.

## Full schema

See [YAML Formats: synth.yaml](../reference/yaml.md#synthyaml) for the complete field reference.
