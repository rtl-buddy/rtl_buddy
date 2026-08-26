## cdc.yaml

Required keys are `rtl-buddy-filetype: cdc_config` and `analyses`.

```yaml
rtl-buddy-filetype: cdc_config
analyses:
  - name: demo_cdc
    desc: CDC analysis
    model: demo_top
    model_path: ../../design/demo/models.yaml
    tool: rtl-buddy-cdc
    constraints: demo_top.sdc
    frontend: slang
    single_unit: true
    reglvl: 0
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Analysis identifier and artefact directory |
| `model` | Required | Model and elaboration top |
| `model_path` | Required | `models.yaml` relative to `cdc.yaml` |
| `tool` | Required | Analyzer and `cfg-cdc-tools` entry |
| `constraints` | Required | SDC path relative to `cdc.yaml` |
| `desc` | Required | Human-readable description |
| `waivers` | Optional | Waiver path relative to `cdc.yaml` |
| `frontend` | Optional | Forwarded analyzer frontend |
| `single_unit` | Default false | Forward `--single-unit` for one preprocessor compilation unit |
| `blackbox` | Optional list | Module names forwarded with `--blackbox` |
| `recognized-syncs` | Optional list | Instance regular expressions accepted as synchronizers |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Optional map | Per-analyzer overrides |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

`rb cdc` produces text and JSON analyzer outputs. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/cli/) for commands and options.
