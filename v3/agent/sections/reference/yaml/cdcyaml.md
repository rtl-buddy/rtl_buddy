## cdc.yaml

**Required keys:**

- `rtl-buddy-filetype: cdc_config`
- `analyses`

**Example:**

```yaml
rtl-buddy-filetype: cdc_config

analyses:
  - name: "ip_cdc_handshake_lint"
    desc: "CDC lint of the request/ack handshake IP"
    model: "ip_cdc_handshake"
    model_path: "../../design/common/models.yaml"
    tool: "rtl-buddy-cdc"
    constraints: "ip_cdc_handshake.sdc"
    waivers: "ip_cdc_handshake.waivers"   # optional
    reglvl: 0

  - name: "alu_accel_lint"
    desc: "CDC lint of the ALU accelerator"
    model: "alu_accel_top"
    model_path: "../../design/alu_accel/models.yaml"
    tool: "rtl-buddy-cdc"
    constraints: "alu_accel_top.sdc"
    reglvl:
      default: 0
      rtl-buddy-cdc: 100
    tool_overrides:
      rtl-buddy-cdc:
        sync_depth: 3
        extra_args: "--strict"
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Analysis identifier; used on the CLI and in `artefacts/{name}/` |
| `desc` | string | Human-readable analysis description |
| `model` | string | Model name from `models.yaml`; also used as the top module for elaboration |
| `model_path` | string | Path to `models.yaml`, resolved relative to the `cdc.yaml` file |
| `tool` | string | CDC tool name from `root_config.yaml` `cfg-cdc-tools` |
| `constraints` | string | SDC file path, resolved relative to the `cdc.yaml` file |
| `waivers` | string | Optional waiver file path, resolved relative to the `cdc.yaml` file |
| `reglvl` | int or dict | Regression level; int for all tools, dict for per-tool with `default` |
| `tool_overrides` | dict | Optional per-tool overrides for `sync_depth` or `extra_args`, keyed by CDC tool name |

**Runtime effects:**

- `rtl-buddy cdc` loads `cdc.yaml`, resolves sources via `models.yaml`, and dispatches to the backend selected by `tool`.
- The bundled `rtl-buddy-cdc` backend invokes the standalone `rtl-buddy-cdc lint` CLI as a subprocess. The analysis receives the model's resolved filelist, the SDC, an optional waivers file, and the merged tool opts (root `cfg-cdc-tools` baseline plus any matching `tool_overrides.<tool>`).
- Each analysis writes a text report and a machine-readable JSON report under `artefacts/{name}/`; the JSON summary is parsed to populate the pass/fail/skip result for the CLI table.
- `rtl-buddy cdc <name> --list` lists configured analyses without running them.

---
