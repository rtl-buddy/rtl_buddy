## Configure portable tool paths

Executable fields accept a bare name, a relative or absolute path, or an ordered list of candidates:

```yaml
cfg-surfer:
  - name: surfer-default
    path:
      - ${RB_TOOLS}/bin/surfer
      - /opt/rb-tools/current/bin/surfer
      - surfer
```

RTL Buddy expands `~` and environment variables and picks the first candidate that exists and is executable. Relative paths resolve from `root_config.yaml`, and a bare name falls back to `PATH`. A candidate containing an unset variable is skipped. A candidate list lets you combine a machine override, a committed shared-tool path, and a `PATH` fallback without editing tracked YAML.

This applies to `cfg-rtl-builder[].builder`, `cfg-surfer[].path`, the tool fields in `cfg-*-tools`, and `cfg-verible[].path`.

`cfg-verible[].path` names a directory, not a binary. A bare value is a directory relative to the root config, not a `PATH` lookup. If that directory lacks a requested Verible executable, RTL Buddy warns and may use the one on `PATH`.
