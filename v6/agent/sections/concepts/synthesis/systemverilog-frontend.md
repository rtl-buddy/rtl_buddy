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
      best-effort-hierarchy: false
```

Build the plugin against the same Yosys installation. `plugin-path` resolves from the project root. If omitted, RTL Buddy checks `RTL_BUDDY_SLANG_PLUGIN`; that environment value must be absolute, although `~` is expanded.

Set `single-unit: true` only when source files intentionally share preprocessor definitions across file boundaries. It applies only to slang; with the Verilog frontend it is ignored with a warning. Non-Boolean values are fatal.

Set `best-effort-hierarchy: true` to forward `read_slang --best-effort-hierarchy`, which keeps module instances as hierarchy instead of inlining them. yosys-slang inlines every instance by default, and a `(* keep_hierarchy *)` attribute does not survive that, so a design that relies on hierarchy for mapping — a combinational leaf built from several `keep_hierarchy` multiplier modules, say, where the flattened cone stalls ABC — needs it. `synth -top` does not flatten, and the area parser already reads the `=== design hierarchy ===` roll-up, so a hierarchical result is reported as usual. Like `single-unit`, it applies only to slang, is ignored with a warning under the Verilog frontend, and is fatal if non-Boolean.

For one run, override the Yosys elaboration stage:

```yaml
tool_overrides:
  yosys:
    frontend: slang
    plugin_path: ../yosys-slang/build/slang.so
    single_unit: true
    best_effort_hierarchy: true
```

Under `cfg-synth-tools.opts`, fields use kebab case such as `plugin-path`, `single-unit` and `best-effort-hierarchy`. Under `tool_overrides.yosys`, use snake case such as `plugin_path`, `single_unit` and `best_effort_hierarchy`. Unknown override keys are warned about and ignored.

The override key remains `yosys` even when the run's backend is `openroad`, because Yosys owns elaboration.
