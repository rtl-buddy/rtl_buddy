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

RTL Buddy expands `~` and environment variables, then chooses the first executable candidate that exists. Relative paths resolve from `root_config.yaml`; a bare name falls back to `PATH`. A candidate containing an unset variable is skipped.

This applies to `cfg-rtl-builder[].builder`, `cfg-surfer[].path`, tool fields in `cfg-*-tools`, and `cfg-verible[].path`. The Verible field names a directory rather than a binary, so a bare value is a root-config-relative directory, not a `PATH` lookup. If the configured directory cannot supply a requested Verible executable, RTL Buddy warns and may use the executable found on `PATH`.

Use candidate lists to combine a machine override, a committed shared-tool path, and a `PATH` fallback without editing tracked YAML.
