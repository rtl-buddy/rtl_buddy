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

### Effort levels

A synthesis can select a named **effort level** that controls how much work the flow does. Efforts are defined once in `root_config.yaml` under `cfg-synth-efforts` (see [below](https://rtl-buddy.github.io/rtl_buddy/v3/concepts/synthesis/#synthesis-effort-configuration)) and referenced per synthesis:

```yaml
syntheses:
  - name: "sandbox_quick"
    desc: "Fast iteration build — Yosys-only, no STA"
    model: "test_module"
    model_path: "../../design/sandbox/models.yaml"
    tool: "openroad"
    libraries:
      - "sky130hd_tt"
    constraints: "constraints.sdc"
    effort: "quick"      # references a cfg-synth-efforts entry
    reglvl: 0
```

When `effort:` is omitted, a built-in `standard` effort with all defaults is used — equivalent to the pre-effort behaviour. The `--effort <name>` CLI flag overrides the per-synthesis setting at runtime:

```bash
rb synth sandbox_openroad --effort quick     # force the quick path
rb synth-regression --effort accurate        # apply across a whole regression
```

Precedence for the same knob: per-synthesis `tool_overrides` > `cfg-synth-efforts` > `cfg-synth-tools`.
