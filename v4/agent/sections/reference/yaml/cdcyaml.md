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
    frontend: "slang"   # opt this analysis into the slang elaboration frontend
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
| `frontend` | string | Optional elaboration frontend selector forwarded as-is via `--frontend <value>` to the analyzer subprocess. The set of accepted values is the analyzer's, not rtl_buddy's — for the bundled `rtl-buddy-cdc` backend on current main it's `"yosys"` (built-in) or `"slang"` (full SV-2017 via the optional `pyslang`-backed `[slang]` extra); see the analyzer's own docs for the authoritative list. Unknown values are rejected by the analyzer, not by rtl_buddy. Omit to use the analyzer's own default. |

**Runtime effects:**

- `rtl-buddy cdc` loads `cdc.yaml`, resolves sources via `models.yaml`, and dispatches to the backend selected by `tool`.
- The bundled `rtl-buddy-cdc` backend invokes the standalone `rtl-buddy-cdc lint` CLI as a subprocess. The analysis receives the model's resolved filelist, the SDC, an optional waivers file, the merged tool opts (root `cfg-cdc-tools` baseline plus any matching `tool_overrides.<tool>`), and — when set — `--frontend <value>` from the per-analysis `frontend` field.
- `frontend` is **per-analysis** (not on `cfg-cdc-tools` opts) — different from the synth side, where the equivalent selector lives on `cfg-synth-tools.opts.frontend`. Per-analysis suits the CDC use case because slang-required and Yosys-only analyses commonly coexist in one suite, and there is no useful project-wide default.
- Each analysis writes a text report and a machine-readable JSON report under `artefacts/{name}/`; the JSON summary is parsed to populate the pass/fail/skip result for the CLI table.
- `rtl-buddy cdc <name> --list` lists configured analyses without running them.

---
