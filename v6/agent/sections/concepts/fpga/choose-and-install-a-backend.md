## Choose and install a backend

Each `fpga.yaml` run selects one backend:

| Tool | Supports | Required setup |
| --- | --- | --- |
| `vivado` | All parts supported by installed Vivado; default. | Vivado executable and any required license. |
| `openxc7` | Xilinx 7-series parts whose names start with `xc7`. | Yosys, nextpnr-xilinx, chip database, and prjxray for bitstreams. |

For Vivado, source the vendor settings before running:

```bash
source /opt/Xilinx/Vivado/<version>/settings64.sh
rb tool-check --explain vivado
```

Alternatively, set an absolute executable in `cfg-fpga-tools` in `root_config.yaml`. Vivado generally belongs on local or licensed lab runners, not public CI.

For openXC7, install the [openXC7 toolchain](https://github.com/openXC7/toolchain-installer) and provide its data paths:

```yaml
runs:
  - name: counter_a35t
    tool: openxc7
    model: fpga_counter
    model_path: ../src/models.yaml
    part: xc7a35tcsg324-1
    xdc: [constraints/arty.xdc]
    tool_overrides:
      openxc7:
        chipdb: /opt/nextpnr-xilinx/xc7a35t.bin
        prjxray_db: /opt/prjxray/database
```

`CHIPDB` may instead point to a directory containing `<part>.bin`; `PRJXRAY_DB_DIR` may provide the prjxray database. The latter is required only for bitstream generation.

A non-7-series part with `tool: openxc7` is a configuration error. Missing tools or databases return SKIP with a `rb tool-check` hint. openXC7 reports utilization, per-clock Fmax, WNS, timing status, and failing paths; power, DRC, methodology, TNS, and hold metrics are absent. Machine consumers must treat metrics as optional.
