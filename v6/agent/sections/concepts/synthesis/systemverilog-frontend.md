## SystemVerilog frontend

Use yosys-slang when the built-in `read_verilog -sv` frontend cannot parse the design:

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      frontend: slang
      plugin-path: ../yosys-slang/build/slang.so
      single-unit: false
```

Build the plugin against the same Yosys installation. `plugin-path` resolves from the project root. If omitted, RTL Buddy checks `RTL_BUDDY_SLANG_PLUGIN`; that environment value must be absolute, although `~` is expanded.

Set `single-unit: true` only when source files intentionally share preprocessor definitions across file boundaries. It applies only to slang; with the Verilog frontend it is ignored with a warning. Non-Boolean values are fatal.

For one run, override the Yosys elaboration stage:

```yaml
tool_overrides:
  yosys:
    frontend: slang
    plugin_path: ../yosys-slang/build/slang.so
    single_unit: true
```

Under `cfg-synth-tools.opts`, fields use kebab case such as `plugin-path` and `single-unit`. Under `tool_overrides.yosys`, use snake case such as `plugin_path` and `single_unit`. Unknown override keys are warned about and ignored.

The override key remains `yosys` even when the run's backend is `openroad`, because Yosys owns elaboration.
