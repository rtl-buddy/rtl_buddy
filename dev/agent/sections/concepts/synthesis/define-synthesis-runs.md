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

Use `lef-paths` and `lib-paths` for block-specific hard macros. Use `tool_overrides` only for backend options that have no portable equivalent. See [YAML Formats: synth.yaml](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#synthyaml) for all fields.
