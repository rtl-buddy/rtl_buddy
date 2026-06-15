---
description: How to run an FPGA implementation flow (synthesis, place, route, optional bitstream) with rtl_buddy via the rb fpga command and fpga.yaml, driving AMD/Xilinx Vivado in batch mode or the open openXC7 toolchain, including an agent-driven timing-closure loop.
---

# FPGA Implementation

> **Integration type:** Pluggable. `rb fpga` ships two backends — `vivado` (default) and `openxc7` (open-source, 7-series only); further backends register in the same backend table.
>
> **External binaries required:** `vivado` — see [Installing Vivado](#installing-vivado) — or, for the open alternative, `yosys` + `nextpnr-xilinx` + prjxray — see [The openXC7 backend](#the-openxc7-backend-open-source-7-series-only).
>
> See also: [Installation — External tools by feature](../install.md#external-tools-by-feature).

`rb fpga` drives a full FPGA implementation flow — RTL synthesis, placement, routing, post-route reports, and (on request) a bitstream — for one target part per run. The value-add over driving the tools by hand: a stable `artefacts/<run>/` layout, structured pass/fail with utilization/timing/power/DRC metrics distilled from multi-thousand-line logs, machine-mode JSON for agents, and the same regression/reglvl model as `rb synth`/`rb pnr`.

## Supported backends

The `tool:` field in `fpga.yaml` selects the backend; an unknown value is a config error (exit 2).

| `tool:` | Engine | Parts | Notes |
|---|---|---|---|
| `vivado` (default) | AMD/Xilinx Vivado | all Vivado-supported | proprietary; full report set |
| `openxc7` | Yosys + nextpnr-xilinx + prjxray | 7-series only (`xc7...`) | open source; reduced metric set |

`vivado` is the default because openXC7 covers only the 7-series families — an open default would break every UltraScale+/Versal platform out of the box. **openXC7 is the open alternative for 7-series parts; select it per run with `tool: openxc7`.** Where `rb synth` can default open because Yosys reads any RTL, an FPGA backend is only usable when it can target your silicon.

Vivado is driven in non-project batch mode:

```text
vivado -mode batch -source flow.tcl -nojournal -log vivado.log
```

with `read_verilog`/`read_xdc` → `synth_design` → `opt_design` → `place_design` → `route_design`, followed by `report_utilization`, `report_timing_summary`, `report_power`, `report_drc`, and `report_methodology`, and finally `write_bitstream` when `--bitstream` is passed.

### The openXC7 backend (open source, 7-series only)

`tool: openxc7` runs the [openXC7](https://github.com/openXC7) flow as a stage pipeline in the same run directory:

1. `yosys -s synth.ys` — `synth_xilinx` to a JSON netlist (`synth.ys` is generated from the model's filelist, like `rb synth`).
2. `nextpnr-xilinx --chipdb <part>.bin --xdc ... --json <top>.json --fasm <top>.fasm` — place and route; utilization and per-clock Fmax come from its log.
3. With `--bitstream` only: prjxray's `fasm2frames` then `xc7frames2bit` produce `<top>.bit`.

Install the whole chain with the [openXC7 toolchain installer](https://github.com/openXC7/toolchain-installer); `rb tool-check` reports `yosys`, `nextpnr-xilinx`, and `prjxray` individually. Beyond binaries the flow needs two data inputs:

- **nextpnr chipdb** — the per-device `.bin`. Point `tool_overrides.openxc7.chipdb` at the file, or set `$CHIPDB` to the directory holding `<part>.bin` files.
- **prjxray database** (bitstream only) — set `tool_overrides.openxc7.prjxray_db` or `$PRJXRAY_DB_DIR` to the database root (containing `artix7/`, `zynq7/`, ...).

```yaml
runs:
  - name: "counter_a35t"
    desc: "counter on an Arty A7-35"
    tool: "openxc7"
    model: "fpga_counter"
    model_path: "../src/models.yaml"
    part: "xc7a35tcsg324-1"
    xdc: ["constraints/arty.xdc"]
    tool_overrides:
      openxc7:
        chipdb: "/opt/nextpnr-xilinx/xc7a35t.bin"
```

A non-7-series part with `tool: openxc7` is a config error (exit 2). Missing binaries or an unconfigured chipdb/database report SKIP with a pointer to `rb tool-check`, exactly like a missing `vivado`.

The result shape is the same `FpgaPassResults` contract, but the open flow measures less: LUT/FF/BRAM/DSP utilization (from nextpnr's device-utilisation log), `fmax_mhz`, `wns_ns` (derived per clock from achieved vs. target frequency), `timing_met`, and `failing_paths`. There is no power, DRC, methodology, TNS, or hold-slack reporting — those keys are simply absent from the payload, never fabricated. Machine consumers must treat every metric key as optional.

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
| `tool` | Backend name (`"vivado"` or `"openxc7"`); defaults to `"vivado"` |
| `part` | Full device part name (e.g. `xczu7ev-ffvc1156-2-e`), passed to `synth_design -part`. Mutually exclusive with `platform` |
| `platform` | Name of a [`cfg-fpga-platforms`](#platforms-cfg-fpga-platforms) entry in `root_config.yaml`. Mutually exclusive with `part` |
| `xdc` | Optional list of XDC constraint files, resolved relative to `fpga.yaml`. With `platform:`, these *extend* the platform's default XDC set (see [XDC ownership](#xdc-ownership-and-ordering)) |
| `reglvl` | Regression level for filtering (same semantics as `rb synth`/`rb pnr`) |
| `require-timing-met` | Optional (default `false`). When `true`, a routed run that misses timing (`timing_met: false`) is a **FAIL** instead of a PASS — see [timing closure](#timing-closure-and-the-default-pass-on-unmet-timing) |
| `xfail` / `xfail_strict` | Expected-failure markers — see [Expected Failures](expected-failures.md) |

Naming both `part:` and `platform:` on one run is a config error (exit 2) — there is no precedence rule.

### Timing closure and the default pass-on-unmet-timing

By default a routed run with negative slack still reports **PASS** — the metrics (`timing_met`, `wns_ns`, `failing_paths`) carry the truth so an agent can drive a [timing-closure loop](#timing-closure-with-an-agent) instead of getting an opaque failure. Pass/fail keys off the flow completing, not off meeting timing, matching `rb pnr`.

Set `require-timing-met: true` on a run to make timing a hard gate (useful for regression suites that must not regress closure). An unmet-timing run then FAILs, but the routed metrics still ride along on the failing result so the loop keeps its inputs. The gate only fires when the backend reports timing — a backend that cannot measure it (`timing_met: null`, e.g. openXC7 without a timing report) is never gated. This behavior is also recorded in [Quirks & Known Issues](../known-issues.md).

## Platforms: `cfg-fpga-platforms`

A platform lifts the device choice out of individual runs into a reusable `root_config.yaml` entry, parallel to `cfg-pnr-platforms` on the ASIC side. This is how one suite sweeps the same RTL across several parts:

```yaml
cfg-fpga-platforms:
  - name: "zu7ev_board"
    part: "xczu7ev-ffvc1156-2-e"
    board: "my-zu7ev-board"        # optional, informational
    xdc:
      - "constraints/board.xdc"    # platform default constraints
  - name: "vu19p"
    part: "xcvu19p-fsva3824-1-e"
```

| Field | Description |
|-------|-------------|
| `name` | Platform identifier referenced by `platform:` in `fpga.yaml` |
| `part` | Full device part name (required) |
| `board` | Optional board name, informational only |
| `package` | Optional package name, informational only — Vivado part names already encode the package (`ffvc1156` inside `xczu7ev-ffvc1156-2-e`), so this field is never re-attached to the part |
| `xdc` | Optional default XDC list (board clocks, pinout), resolved relative to `root_config.yaml` |

`fpga.yaml` runs then reference a platform instead of naming a part:

```yaml
runs:
  - name: "counter_zu7ev"
    desc: "counter on the ZU7EV board"
    model: "fpga_counter"
    model_path: "../src/models.yaml"
    platform: "zu7ev_board"
    xdc:
      - "constraints/counter_timing.xdc"   # extends the platform set
```

### XDC ownership and ordering

The platform owns the *default* constraint set (board-level clocks and pinout); `fpga.yaml` owns per-run selection — the same split as `pnr.yaml` owning the floorplan while `cfg-pnr-platforms` owns the technology. Per-run `xdc:` entries **extend** (never replace) the platform's list, and the read order is platform files first, run files after. Vivado applies XDC in read order with later commands winning, so a run-level constraint overrides a platform default for the same object.

## Regression: `rb fpga-regression`

Multiple `fpga.yaml` suites aggregate under one `fpga_regression.yaml`, exactly like `rb synth-regression` / `rb power-regression`:

```yaml
rtl-buddy-filetype: fpga_reg_config

fpga-configs:
  - "blocks/counter/fpga.yaml"
  - "blocks/fifo/fpga.yaml"
```

```bash
# All suites, reglvl-0 entries only (the default)
rb fpga-regression

# Deeper sweep: include entries up to reglvl 1000
rb fpga-regression -l 1000

# Explicit config path + bitstreams
rb fpga-regression -c ci/fpga_regression.yaml --bitstream
```

Each run's `reglvl:` is compared against `-l/--reg-level`; entries above the level are reported SKIP. In machine mode every result row carries a `suite` key naming the originating `fpga.yaml`, and the envelope command is `fpga-regression`.

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
        "dynamic_power_w": 0.044,
        "static_power_w": 0.592,
        "drc_violations": 3,
        "drc_by_severity": {"Critical Warning": 2, "Warning": 1},
        "methodology_warnings": [
          {"id": "TIMING-18#1", "severity": "Warning",
           "description": "Missing input or output delay"}
        ],
        "bitstream": null
      }
    ]
  }
}
```

On a run that completed but missed timing, the payload additionally carries the [timing-closure loop fields](#timing-closure-with-an-agent): `timing_met: false`, the negative `wns_ns`/`tns_ns`, `failing_endpoints`, and `failing_paths` — the worst violated paths with their start/endpoints.

## Timing closure with an agent

Failing timing is **not** a flow failure: the run completes, exits 0, and the machine payload carries everything a closure loop needs. The loop an agent (or a human with `jq`) drives:

1. **Run** in machine mode: `rb --machine fpga <run>`.
2. **Read** `timing_met`. If `true`, done — record `wns_ns` as margin. If `false`, read `wns_ns`, `tns_ns`, `failing_endpoints`, and `failing_paths`.
3. **Hypothesize** from the worst path's shape:
    - `requirement_ns` far below what the logic can do → the clock constraint is simply too fast; relax `create_clock -period` to ≈ `requirement_ns - wns_ns` (the implied achievable period).
    - Source and destination in different clock domains or a quasi-static config path → a missing `set_false_path` / `set_multicycle_path` exception.
    - High `logic_levels` and `data_path_delay_ns` dominated by logic → a pipeline candidate: add a register stage between the path's `source` and `destination`.
    - Delay dominated by routing → congestion; look at utilization and placement constraints.
4. **Edit** the XDC (constraint fixes) or the RTL (pipelining), then **rerun** the same command.
5. **Compare** `wns_ns` across iterations; stop when `timing_met` flips or improvement stalls.

A worked example, from a deliberately over-constrained run of the small counter/multiplier design the test fixtures are generated from (`xczu7ev`, `create_clock -period 0.050` — 20 GHz, hopeless by construction):

```json
{
  "timing_met": false,
  "wns_ns": -0.882,
  "tns_ns": -81.047,
  "failing_endpoints": 101,
  "failing_paths": [
    {
      "slack_ns": -0.882,
      "source": "product_reg/DSP_A_B_DATA_INST/CLK",
      "destination": "product_reg/DSP_M_DATA_INST/V[0]",
      "path_group": "clk",
      "path_type": "Setup",
      "requirement_ns": 0.05,
      "data_path_delay_ns": 0.894,
      "logic_levels": 2,
      "met": false
    }
  ]
}
```

Reading it: the worst path is *inside the DSP48 multiplier* (`product_reg/DSP_*`), only 2 logic levels, 0.894 ns of pure logic delay against a 0.05 ns requirement. No XDC exception applies and no pipelining can beat the DSP's internal delay — the only fix is the constraint: the implied achievable period is `0.05 - (-0.882) ≈ 0.93 ns` plus margin, so set `create_clock -period 1.1` (≈900 MHz) and rerun; alternatively enable the DSP's `MREG`/`PREG` pipeline registers in RTL and accept the latency. With the relaxed clock the same design closes with `wns_ns: 8.452` at a 10 ns period (the passing fixture).

On `openxc7` the same loop reads `timing_met`, `wns_ns`, `fmax_mhz`, and `failing_paths` (each entry carries `clock`, achieved `fmax_mhz` vs `target_mhz`, derived `slack_ns`, and the critical path's `source`/`destination`); `failing_endpoints` and per-path requirement/levels are not available.

## Artefacts

Per-run outputs land under `<suite>/artefacts/<run>/`. With `tool: vivado`:

| File | Contents |
|---|---|
| `fpga.f` | Generated model filelist |
| `flow.tcl` | Rendered batch-Tcl flow handed to Vivado |
| `vivado.log` | Full Vivado log |
| `util.rpt` | `report_utilization` |
| `timing_summary.rpt` | `report_timing_summary` |
| `power.rpt` | `report_power` |
| `drc.rpt` | `report_drc` |
| `methodology.rpt` | `report_methodology` |
| `<top>.bit` | Bitstream — only with `--bitstream` |

With `tool: openxc7`:

| File | Contents |
|---|---|
| `fpga.f` | Generated model filelist |
| `synth.ys` | Generated Yosys synthesis script |
| `yosys.log` | Yosys log |
| `<top>.json` | Yosys JSON netlist |
| `nextpnr.log` | nextpnr-xilinx log (utilization + timing source) |
| `<top>.fasm` | Routed FASM |
| `fasm2frames.log`, `xc7frames2bit.log`, `<top>.frames`, `<top>.bit` | Bitstream stages — only with `--bitstream` |

## Power

`report_power` runs post-route on every pass, and the results carry the headline watts: `total_power_w` (total on-chip), `dynamic_power_w`, and `static_power_w` (device static). This is the FPGA realization of [issue #103](https://github.com/rtl-buddy/rtl_buddy/issues/103) — where `rb power` answers "what does this netlist burn on an ASIC flow", `rb fpga` answers the same question for an FPGA target, from the vendor's own post-route power model. The human summary table shows the total; machine mode carries all three.

Treat the absolute numbers with the report's own caveats: a post-route vector-less estimate (the raw `power.rpt` records Vivado's confidence level, typically *Low* without simulation activity data) is for trend-tracking across runs, not signoff.

## Methodology warnings

`report_methodology` runs alongside the other post-route reports and its findings surface as `methodology_warnings` — a list of `{id, severity, description}` entries with Vivado's rule ids (`TIMING-18`, `SYNTH-*`, ...) and severities kept verbatim. The same framing as the [CDC second-opinion backend](cdc.md#vivado-backend-second-opinion-not-authority) applies: these are vendor findings rtl_buddy surfaces, not a ruleset it adopts as canonical — they are **informational and never flip pass/fail**. The human summary shows the count (the `Meth` column); machine mode carries the full list; the raw report stays in `methodology.rpt` for digging.

## Pass/fail detection

A run is PASS when:

1. The tool exits with code 0 (on `openxc7`: every pipeline stage).
2. The log has no error lines (`ERROR: [...]` for Vivado, `ERROR:` for the open tools).
3. All reports/logs were produced and parse.
4. With `--bitstream`: the `.bit` file exists.

Otherwise FAIL is returned with the cause (on `openxc7`, naming the failed stage) in the description. SKIP is returned when the backend's tooling is not installed/configured or when the run's `reglvl` is above the `-l` filter. Note that failing timing is **not** a FAIL by itself — the run completes and `wns_ns` / `timing_met` carry the truth, so the [timing-closure loop](#timing-closure-with-an-agent) can read the metrics and iterate.

## Out of scope (today)

- Include-directory (`+incdir+`) propagation into `synth_design` / `synth_xilinx`.
- Vivado's `report_cdc` is integrated as a second-opinion backend of [`rb cdc`](cdc.md#vivado-backend-second-opinion-not-authority), not of `rb fpga`.
