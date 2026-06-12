---
description: How to run an FPGA implementation flow (synthesis, place, route, optional bitstream) with rtl_buddy via the rb fpga command and fpga.yaml, driving AMD/Xilinx Vivado in batch mode.
---

# FPGA Implementation

> **Integration type:** Pluggable. `rb fpga` is built around Vivado today; further backends register in the same backend table.
>
> **External binary required:** `vivado` — see [Installing Vivado](#installing-vivado).
>
> See also: [Installation — External tools by feature](../install.md#external-tools-by-feature).

`rb fpga` drives a full FPGA implementation flow — RTL synthesis, placement, routing, post-route reports, and (on request) a bitstream — for one target part per run. The value-add over driving Vivado by hand: a stable `artefacts/<run>/` layout, structured pass/fail with utilization/timing/power/DRC metrics distilled from multi-thousand-line logs, machine-mode JSON for agents, and the same regression/reglvl model as `rb synth`/`rb pnr`.

## Supported backend

Today only `vivado` is wired up; it is also the default when `tool:` is omitted. The `tool:` field in `fpga.yaml` selects the backend; an unknown value is a config error (exit 2). Vivado is driven in non-project batch mode:

```text
vivado -mode batch -source flow.tcl -nojournal -log vivado.log
```

with `read_verilog`/`read_xdc` → `synth_design` → `opt_design` → `place_design` → `route_design`, followed by `report_utilization`, `report_timing_summary`, `report_power`, and `report_drc`, and finally `write_bitstream` when `--bitstream` is passed.

## Installing Vivado

Download the installer from the [AMD/Xilinx download page](https://www.xilinx.com/support/download.html) and source the settings script so `vivado` lands on `PATH`:

```bash
source /opt/Xilinx/Vivado/<version>/settings64.sh
```

Alternatively, pin an absolute path via a `cfg-fpga-tools` entry in `root_config.yaml`:

```yaml
cfg-fpga-tools:
  - name: "vivado"
    tool: "/opt/Xilinx/Vivado/2022.1/vivado"
```

`rb tool-check` reports the detected Vivado and its version. If `vivado` is not found at run time, the run is reported as SKIP (not FAIL) — the feature is optional and opt-in.

### Licensing and CI caveats

Vivado is proprietary: it cannot run in public CI, and larger parts require a purchased license served at runtime (the free ML/WebPACK device set covers smaller parts). rtl_buddy's own test suite therefore never invokes Vivado — the flow is tested by contract against sanitized report fixtures. Treat `rb fpga` runs as local/lab jobs, and gate them in regressions with `reglvl:` so license-less environments skip them cleanly.

## FPGA config: `fpga.yaml`

`fpga.yaml` declares one or more implementation runs. Each entry references a model from a `models.yaml` (which supplies the filelist and the top module name) and names the target part:

```yaml
rtl-buddy-filetype: fpga_config

runs:
  - name: "demo_fpga"
    desc: "Counter on a ZU7EV"
    tool: "vivado"
    model: "fpga_counter"
    model_path: "../src/models.yaml"
    part: "xczu7ev-ffvc1156-2-e"
    xdc:
      - "constraints/clocks.xdc"
    reglvl: 1000
```

### Fields

| Field | Description |
|-------|-------------|
| `name` | Run identifier used on the command line and in `artefacts/<name>/` |
| `desc` | Human-readable description |
| `model` | Model name from `model_path`'s `models.yaml`; the model name is the top module |
| `model_path` | Path to the `models.yaml` defining `model`, resolved relative to `fpga.yaml` |
| `tool` | Backend name; defaults to `"vivado"` |
| `part` | Full device part name (e.g. `xczu7ev-ffvc1156-2-e`), passed to `synth_design -part` |
| `xdc` | Optional list of XDC constraint files, resolved relative to `fpga.yaml` |
| `reglvl` | Regression level for filtering (same semantics as `rb synth`/`rb pnr`) |
| `xfail` / `xfail_strict` | Expected-failure markers — see [Expected Failures](expected-failures.md) |

## Running

```bash
# All runs in the default ./fpga.yaml
rb fpga

# A single run from a specific config
rb fpga demo_fpga -c fpga/demo/fpga.yaml

# Generate the bitstream too (off by default — a smoke/timing run
# doesn't need the extra bitgen minutes)
rb fpga demo_fpga --bitstream

# Reglvl-gated runs
rb fpga demo_fpga -l 1000

# List runs without executing
rb fpga --list
```

Without `--bitstream` the flow stops after the post-route reports and the results carry `bitstream: null`.

With `--bitstream`, the two bitgen-blocking I/O DRCs (`NSTD-1` unspecified IOSTANDARD, `UCIO-1` unconstrained pin location) are downgraded to warnings just before `write_bitstream` — `rb fpga` targets IP-level models that usually carry no board pinout, and `report_drc` still records both at their original severity. Board projects that constrain every pin are unaffected.

## Machine mode

With the global `--machine` flag the command emits a single JSON envelope on stdout. The per-run payload carries the post-route metrics:

```json
{
  "command": "fpga",
  "exit_code": 0,
  "meta": {"rtl_buddy_version": "...", "argv": ["..."], "cwd": "...", "git": {}},
  "payload": {
    "results": [
      {
        "name": "demo_fpga",
        "result": "PASS",
        "desc": "FPGA flow passed",
        "lut": {"used": 1, "fixed": 0, "available": 230400, "util_pct": 0.01},
        "ff": {"used": 16, "fixed": 0, "available": 460800, "util_pct": 0.01},
        "bram": {"used": 0.5, "fixed": 0, "available": 312, "util_pct": 0.16},
        "dsp": {"used": 1, "fixed": 0, "available": 1728, "util_pct": 0.06},
        "wns_ns": 8.452,
        "tns_ns": 0.0,
        "whs_ns": 0.059,
        "timing_met": true,
        "total_power_w": 0.636,
        "drc_violations": 3,
        "drc_by_severity": {"Critical Warning": 2, "Warning": 1},
        "bitstream": null
      }
    ]
  }
}
```

## Artefacts

Per-run outputs land under `<suite>/artefacts/<run>/`:

| File | Contents |
|---|---|
| `fpga.f` | Generated model filelist |
| `flow.tcl` | Rendered batch-Tcl flow handed to Vivado |
| `vivado.log` | Full Vivado log |
| `util.rpt` | `report_utilization` |
| `timing_summary.rpt` | `report_timing_summary` |
| `power.rpt` | `report_power` |
| `drc.rpt` | `report_drc` |
| `<top>.bit` | Bitstream — only with `--bitstream` |

## Pass/fail detection

A run is PASS when:

1. `vivado` exits with code 0.
2. The log has no `ERROR: [...]` lines.
3. All four post-route reports were produced and parse.
4. With `--bitstream`: the `.bit` file exists.

Otherwise FAIL is returned with the cause in the description. SKIP is returned when `vivado` is not installed or when the run's `reglvl` is above the `-l` filter. Note that failing timing is **not** a FAIL by itself — the run completes and `wns_ns` / `timing_met` carry the truth, so a timing-closure loop can read the metrics and iterate.

## Out of scope (today)

- Platform/board abstraction (`part:` is named directly per run; a `cfg-fpga-platforms` block is planned).
- Include-directory (`+incdir+`) propagation into `synth_design`.
- Methodology/CDC report integration and an openXC7 open-source backend (planned follow-ups).
