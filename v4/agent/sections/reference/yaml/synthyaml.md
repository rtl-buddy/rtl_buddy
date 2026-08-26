## synth.yaml

**Required keys:**

- `rtl-buddy-filetype: synth_config`
- `syntheses`

**Example:**

```yaml
rtl-buddy-filetype: synth_config

syntheses:
  - name: "smoke_synth"
    desc: "Synthesize my_design with the default Yosys flow"
    model: "my_design"
    model_path: "../src/models.yaml"
    tool: "yosys"
    reglvl: 0

  - name: "sky130_synth"
    desc: "Technology-mapped synthesis for SKY130 (Yosys)"
    model: "my_design"
    model_path: "../src/models.yaml"
    tool: "yosys"
    constraints: "constraints.sdc"
    platform: "sky130hd_tt"
    params:
      WIDTH: 32
    defines:
      TARGET_SYNTH: 1
    reglvl:
      default: 0
      dc: 1000
    tool_overrides:
      yosys:
        synth_args: "-flatten"

  - name: "sky130_openroad"
    desc: "Technology-mapped synthesis with OpenROAD timing analysis"
    model: "my_design"
    model_path: "../src/models.yaml"
    tool: "openroad"
    constraints: "constraints.sdc"
    platform: "sky130hd_tt"
    effort: "accurate"     # references cfg-synth-efforts entry; overridable via --effort
    reglvl: 0
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Synthesis identifier; used on the CLI and in `artefacts/{name}/` |
| `desc` | string | Human-readable synthesis description |
| `model` | string | Model name from `models.yaml`; also used as the Yosys top module |
| `model_path` | string | Path to `models.yaml`, resolved relative to the `synth.yaml` file |
| `tool` | string | Synthesis tool name from `root_config.yaml` `cfg-synth-tools` |
| `constraints` | string | Optional SDC file path, resolved relative to the `synth.yaml` file |
| `params` | dict | Optional top-level parameter overrides passed through Yosys `chparam -set` |
| `defines` | dict | Optional Verilog defines passed to `read_verilog` as `-D KEY=VALUE` |
| `platform` | string | Optional `cfg-synth-platforms` name (which references a `cfg-pdks` entry); enables technology mapping |
| `lef-paths` | list of strings | Optional block-specific LEF files (paths resolved relative to the `synth.yaml` file); appended after the PDK's tech/macro LEFs for the OpenROAD backend |
| `reglvl` | int or dict | Regression level; int for all tools, dict for per-tool with `default` |
| `tool_overrides` | dict | Optional per-tool overrides for `synth_args`, `abc_args`, or `strategy`, keyed by synthesis tool name |
| `effort` | string | Optional effort name from `cfg-synth-efforts`; controls Yosys synth/abc args and OpenROAD `pre-sta-tcl`. Overridable per invocation with `rtl-buddy synth --effort <name>`. Omitted ⇒ built-in `standard` defaults. |

**Runtime effects:**

- `rtl-buddy synth` loads `synth.yaml`, resolves sources via `models.yaml`, and dispatches to the backend selected by `tool`.
- **Yosys backend** (`tool: "yosys"`): writes `synth.f` and `synth.ys`, runs Yosys, captures output in `synth.log`. Without `platform`, emits RTLIL; with `platform`, runs `dfflibmap` + `abc -liberty` and emits `synth_netlist.v`. Reports Gates, Area (lib-mapped only), and WNS (lib-mapped with SDC). Passes when exit code is 0 and `synth.log` has no `ERROR:` lines.
- **OpenROAD backend** (`tool: "openroad"`): requires `platform` pointing at a `cfg-synth-platforms` entry whose PDK has `tech-lef` / `macro-lef` set. Stage 1 runs Yosys to produce `synth_netlist.v` (logged to `synth_yosys.log`). Stage 2 runs OpenROAD with `synth.tcl` which calls `read_lef`, `read_liberty`, `read_verilog`, `link_design`, `read_sdc` (native multi-clock), and reports area/timing; output in `synth.log`. Reports Gates, Area, WNS (from `report_checks -path_delay max`), and TNS (from `report_tns`). Passes when both stages exit with code 0 and neither log contains errors.
- If `constraints` contains `create_clock` entries, the Yosys backend uses the minimum period as ABC's `-D` constraint (multi-clock workaround). The OpenROAD backend passes the full SDC to `read_sdc` without modification.
- `effort` selects an entry from `root_config.yaml` `cfg-synth-efforts`. If the selected effort has `openroad.run: false`, a synthesis with `tool: openroad` falls back to the Yosys-only backend (no LEF/STA required) — this is the recommended "quick" path for iteration. The `--effort` CLI flag on `rtl-buddy synth` and `rtl-buddy synth-regression` overrides whatever is set per-synthesis.

---
