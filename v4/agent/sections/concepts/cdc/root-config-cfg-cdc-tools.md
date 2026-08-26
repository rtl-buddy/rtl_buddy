## Root config: `cfg-cdc-tools`

`cfg-cdc-tools` declares the CDC tools available to all suites in this project:

```yaml
cfg-cdc-tools:
  - name: "rtl-buddy-cdc"
    tool: "rtl-buddy-cdc"           # binary on PATH, or absolute path
    opts:
      sync-depth: 2                 # optional — passed via --sync-depth
      extra-args: ""                # optional — appended verbatim
```

| Field | Description |
|-------|-------------|
| `name` | Referenced by `tool:` in `cdc.yaml` |
| `tool` | Binary name (PATH-resolved) or absolute path |
| `opts.sync-depth` | Default synchronizer depth, forwarded via `--sync-depth` |
| `opts.extra-args` | Passed through verbatim to the analyzer command line |
