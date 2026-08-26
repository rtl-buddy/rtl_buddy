## CDC config: `cdc.yaml`

`cdc.yaml` declares one or more CDC analyses. Each entry references a model from `models.yaml`, an SDC describing the clocks, and an optional waivers file:

```yaml
rtl-buddy-filetype: cdc_config

analyses:
  - name: "demo_cdc_full"
    desc: "Full-design CDC lint, no waivers"
    tool: "rtl-buddy-cdc"
    model: "demo_top"
    model_path: "../../design/demo_top/models.yaml"
    constraints: "demo_top.sdc"
    waivers: "demo_top_waivers.yaml"     # optional
    frontend: "slang"                    # optional — overrides default
    reglvl: 1000
```

### Fields

| Field | Description |
|-------|-------------|
| `name` | Analysis identifier used on the command line and in `artefacts/<name>/` |
| `desc` | Human-readable description |
| `tool` | Backend tool name — must match a `cfg-cdc-tools` entry (only `rtl-buddy-cdc` today) |
| `model` | Model name from `models.yaml` |
| `model_path` | Path to `models.yaml`, resolved relative to `cdc.yaml` |
| `constraints` | SDC path (required), resolved relative to `cdc.yaml` |
| `waivers` | Optional waivers YAML, resolved relative to `cdc.yaml` |
| `frontend` | Optional parser frontend — forwarded as-is to `rtl-buddy-cdc --frontend` (rtl_buddy does not validate the set so the analyzer can add frontends without an rtl_buddy release) |
| `reglvl` | Regression level for filtering, or a dict `{tool_name: level, default: level}` |
| `tool_overrides` | Optional per-tool overrides (e.g. `sync_depth`, `extra_args`), keyed by tool name |

### Where inputs come from

The runner reads the model's filelist via `VlogFilelist` (the same helper `rb synth` and `rb fpv` use), strips down to plain source paths (no `+incdir+`, no `-y`, no `-f`), and passes them as positional arguments to `rtl-buddy-cdc lint` alongside `--top`, `--sdc`, and optionally `--waivers`. The top module is taken from the model's `name:` field.
