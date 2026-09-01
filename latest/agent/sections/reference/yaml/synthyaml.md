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
| `lef-paths` / `lib-paths` | Optional lists | Block-specific LEF/Liberty files appended after platform data |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Optional map | Per-tool snake-case overrides: `synth_args`, `abc_args`, `strategy`, `frontend`, `plugin_path`, `single_unit`, `static_functions`, `conflicting_drivers` |
| `effort` | Default `standard` | `cfg-synth-efforts` entry; CLI `--effort` wins |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

`tool: yosys` writes RTLIL without a platform and a mapped netlist with one. `tool: openroad` requires platform LEF data and runs Yosys elaboration before OpenROAD timing analysis. An effort with `openroad.run: false` uses only the Yosys stage. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/).
