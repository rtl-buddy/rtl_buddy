## Unknown synthesis overrides are ignored after a warning

`synth.yaml` `tool_overrides` uses snake_case keys such as `plugin_path` and `single_unit`, unlike the kebab-case names under `cfg-synth-tools.opts`. An unknown key logs the warning `synth_tool_config.unknown_override` and the default is used. A non-mapping block, or a non-boolean `single_unit` or `best_effort_hierarchy`, is fatal. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/).
