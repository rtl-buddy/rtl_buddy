## fpga.yaml

Required keys are `rtl-buddy-filetype: fpga_config` and `runs`.

```yaml
rtl-buddy-filetype: fpga_config
runs:
  - name: demo_fpga
    desc: Counter implementation
    model: fpga_counter
    model_path: ../src/models.yaml
    part: xc7a35tcsg324-1
    xdc: [constraints/clock.xdc]
    reglvl: 1000
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Run identifier and artefact directory |
| `desc` | Required | Human-readable description |
| `model` | Required | Model name and implementation top |
| `model_path` | Required | `models.yaml` path relative to `fpga.yaml` |
| `part` | Exactly one of part/platform | Complete device part declared in the run |
| `platform` | Exactly one of part/platform | `cfg-fpga-platforms` entry supplying the part and default XDC |
| `tool` | Default `vivado` | Registered backend: `vivado` or `openxc7`; unknown values are fatal |
| `xdc` | Default empty | Run-specific constraint paths relative to `fpga.yaml` |
| `reglvl` | Default 0 | Regression level |
| `tool_overrides` | Optional map | Backend-specific overrides keyed by tool name |
| `require-timing-met` | Default false | Fail a passing routed run when the backend explicitly reports timing unmet; no effect when timing status is unavailable |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

Setting both `part` and `platform`, or neither, is fatal. A platform requires `root_config.yaml`; its XDC files are read first and the run's files afterward.

For `openxc7`, `tool_overrides.openxc7` accepts `chipdb`, `prjxray_db`, `yosys`, `nextpnr`, `fasm2frames`, and `xc7frames2bit`. `CHIPDB` and `PRJXRAY_DB_DIR` provide the database fallbacks. The openXC7 backend accepts only Xilinx 7-series parts. See [FPGA Implementation](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/fpga/) for setup, commands, and result metrics.
