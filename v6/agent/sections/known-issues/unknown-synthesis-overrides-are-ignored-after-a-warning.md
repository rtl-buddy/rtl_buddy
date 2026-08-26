## Unknown synthesis overrides are ignored after a warning

`synth.yaml` `tool_overrides` uses snake_case keys such as `plugin_path` and `single_unit`, unlike the kebab-case names under `cfg-synth-tools.opts`. An unknown key logs `synth_tool_config.unknown_override` and the run uses the default. A non-mapping override block or non-boolean `single_unit` is fatal. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/).
