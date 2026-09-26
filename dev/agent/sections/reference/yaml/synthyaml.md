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
| `blocks` | Optional list | Hardened blocks the design instances. Each is `name` (the module), `pnr` (a `harden: true` P&R run) and `pnr-path` (its `pnr.yaml`, relative to `synth.yaml`, required). The abstract's `.lib` and `.lef` are appended to `lib-paths` / `lef-paths`; the model's filelist must still leave the module a blackbox. See [Assemble hardened blocks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#assemble-hardened-blocks) |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Optional map | Per-tool snake-case overrides: `synth_args`, `abc_args`, `strategy`, `frontend`, `plugin_path`, `single_unit`, `best_effort_hierarchy`, `static_functions`, `conflicting_drivers` |
| `effort` | Default `standard` | `cfg-synth-efforts` entry; CLI `--effort` wins |
| `threads` | Default unset (1) | OpenROAD worker threads for the `tool: openroad` timing stage, as in `pnr.yaml`; no effect on Yosys. See [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#openroad-threads) |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

`tool: yosys` writes RTLIL without a platform and a mapped netlist with one. `tool: openroad` requires platform LEF data and runs Yosys elaboration before OpenROAD timing analysis. An effort with `openroad.run: false` uses only the Yosys stage. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/).
