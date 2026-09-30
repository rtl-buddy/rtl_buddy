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

Build the plugin against the same Yosys installation. `plugin-path` resolves from the project root. If omitted, `RTL_BUDDY_SLANG_PLUGIN` is used; it must be an absolute path, though `~` is expanded. An unknown `frontend` or a missing plugin is a configuration error (exit 2) reported before synthesis starts, not a synthesis `FAIL`.

- `single-unit: true` shares preprocessor definitions across source files. Set it only when the sources rely on that.
- `best-effort-hierarchy: true` keeps module instances as hierarchy and honours `(* keep_hierarchy *)`. Without it yosys-slang inlines every instance. Set it when mapping needs the hierarchy, for example when a flattened cone of `keep_hierarchy` multipliers stalls ABC.

Both apply only to slang; the Verilog frontend ignores them with a warning. A non-Boolean value is fatal.

To change the elaboration stage for one run, use `tool_overrides`:

```yaml
tool_overrides:
  yosys:
    frontend: slang
    plugin_path: ../yosys-slang/build/slang.so
```

`cfg-synth-tools.opts` uses kebab case (`plugin-path`); `tool_overrides.yosys` uses snake case (`plugin_path`). Unknown override keys are warned about and ignored. The override key stays `yosys` when the backend is `openroad`.
