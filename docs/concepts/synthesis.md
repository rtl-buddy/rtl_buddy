---
description: How to run synthesis flows with rtl_buddy using synth.yaml, cfg-synth-tools, and the rb synth command.
---

# Synthesis

`rtl_buddy` provides a tool-agnostic synthesis flow that mirrors the simulation workflow. Synthesis runs are described in `synth.yaml` files; tool-specific defaults live in `root_config.yaml` under `cfg-synth-tools`.

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
  - name: "sandbox_synth"
    desc: "Synthesize sandbox module with Yosys"
    model: "test_module"
    model_path: "../../design/sandbox/models.yaml"
    tool: "yosys"
    constraints: "constraints.sdc"
    params:
      WIDTH: 8
    defines:
      TARGET_SYNTH: 1
    reglvl: 0
    tool_overrides:
      yosys:
        abc_args: "-fast"
```

### Synthesis fields

| Field | Description |
|-------|-------------|
| `name` | Run identifier used on the command line and in artefact paths |
| `desc` | Human-readable description |
| `model` | Model name from `models.yaml`; also used as the synthesis top module |
| `model_path` | Path to `models.yaml`, resolved relative to the `synth.yaml` directory |
| `tool` | Synthesis tool name — must match a `cfg-synth-tools` entry in `root_config.yaml` |
| `constraints` | Optional SDC constraints file, resolved relative to `synth.yaml` |
| `params` | Optional key-value pairs passed as top-level parameter overrides (`chparam` in Yosys) |
| `defines` | Optional compile-time Verilog defines passed via `-D KEY=VALUE` |
| `reglvl` | Regression level (int or per-tool dict); same semantics as simulation `reglvl` |
| `tool_overrides` | Optional per-tool option overrides — keyed by tool name, merges over `cfg-synth-tools` defaults |

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

`tool_overrides` is an escape hatch for tool-specific options that don't have a tool-agnostic equivalent. Keys within each tool entry match the `opts` fields defined in `cfg-synth-tools`:

```yaml
tool_overrides:
  yosys:
    synth_args: "-flatten -nordff"
    abc_args: "-fast"
```

Overrides merge over the root-config defaults for that tool.

## Tool configuration: `root_config.yaml`

Synthesis tool defaults are defined under `cfg-synth-tools` in `root_config.yaml`:

```yaml
cfg-synth-tools:
  - name: "yosys"
    tool: "yosys"        # executable name (must be on PATH)
    opts:
      synth-args: ""
      abc-args: ""
```

Multiple tools can be listed. The `tool` field in `synth.yaml` selects which entry to use.

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
rtl-buddy synth sandbox_synth -c synth/sandbox/synth.yaml
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

## Artefacts

Synthesis artefacts land under `artefacts/<synth_name>/` relative to the `synth.yaml` directory:

| File | Contents |
|------|----------|
| `synth.f` | Generated source filelist (resolved from `models.yaml`) |
| `synth.ys` | Generated Yosys script |
| `synth.log` | Captured tool stdout and stderr |
| `synth.rtlil` | Output netlist (RTLIL format) |

## Pass/fail detection

A synthesis run is marked **PASS** when:

1. The tool exits with code 0, **and**
2. No lines starting with `ERROR:` appear in `synth.log`.

Any other outcome is **FAIL** with a description in the results table.

## Full schema

See [YAML Formats: synth.yaml](../reference/yaml.md#synthyaml) for the complete field reference.
