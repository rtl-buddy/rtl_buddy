---
description: Configure and run FPGA synthesis, placement, routing, reports, and optional bitstream generation with Vivado or openXC7.
---

# FPGA Implementation

`rb fpga` implements a model for one FPGA part, writes post-route reports, and returns structured utilization and timing metrics. Add `--bitstream` only when you need a programming file.

## Choose and install a backend

Each `fpga.yaml` run selects one backend:

| Tool | Supports | Required setup |
| --- | --- | --- |
| `vivado` (default) | All parts supported by the installed Vivado | Vivado executable and any required license |
| `openxc7` | Xilinx 7-series parts whose names start with `xc7` | Yosys, nextpnr-xilinx, chip database, and prjxray for bitstreams |

For Vivado, source the vendor settings, or set an absolute executable in `cfg-fpga-tools` in `root_config.yaml`:

```bash
source /opt/Xilinx/Vivado/<version>/settings64.sh
rb tool-check --explain vivado
```

Vivado generally belongs on local or licensed lab runners, not public CI.

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

`CHIPDB` may instead point to a directory containing `<part>.bin`, and `PRJXRAY_DB_DIR` may supply the prjxray database, which only bitstream generation needs.

A non-7-series part with `tool: openxc7` is a configuration error. Missing tools or databases return SKIP with a `rb tool-check` hint. openXC7 reports utilization, per-clock Fmax, WNS, timing status and failing paths. It reports no power, DRC, methodology, TNS or hold metrics, so machine consumers must treat metrics as optional.

## Configure `fpga.yaml`

Each run references a model and either a part or a platform:

```yaml
rtl-buddy-filetype: fpga_config

runs:
  - name: demo_fpga
    desc: Counter on a ZU7EV
    tool: vivado
    model: fpga_counter
    model_path: ../src/models.yaml
    part: xczu7ev-ffvc1156-2-e
    xdc: [constraints/clocks.xdc]
    reglvl: 1000
    require-timing-met: true
```

`model_path` and XDC paths are relative to `fpga.yaml`. The synthesis top is the model's root module (its `top:` in `models.yaml`, defaulting to the model name), and it names the bitstream `<top>.bit`. `part` and `platform` are mutually exclusive; naming both is an exit-2 configuration error. Expected-failure fields follow [Expected Failures](expected-failures.md).

`require-timing-met` defaults to false, so a completed route that misses timing passes while reporting `timing_met: false`, negative slack and failing paths. Set it to true to make timing closure a regression gate; the failing result still carries the timing metrics. A backend that reports no timing result is not failed by this option.

## Share parts with platforms

`cfg-fpga-platforms` in `root_config.yaml` holds part and board constraints shared by several runs:

```yaml
# root_config.yaml
cfg-fpga-platforms:
  - name: zu7ev_board
    part: xczu7ev-ffvc1156-2-e
    board: my-zu7ev-board
    xdc: [constraints/board.xdc]
```

```yaml
# fpga.yaml
runs:
  - name: counter_zu7ev
    model: fpga_counter
    model_path: ../src/models.yaml
    platform: zu7ev_board
    xdc: [constraints/counter_timing.xdc]
```

Platform XDC files are read first and run-level XDC files after them, so run-level commands can override platform defaults. Platform paths resolve from `root_config.yaml`; run paths resolve from `fpga.yaml`.

## Run one suite or a regression

```bash
rb fpga
rb fpga demo_fpga -c fpga/demo/fpga.yaml
rb fpga demo_fpga --bitstream
rb fpga --list
rb fpga-regression -c ci/fpga_regression.yaml -l 1000
```

Without `--bitstream`, the flow stops after routing and reports, and `bitstream` is `null`. When a bitstream is requested, Vivado downgrades the IP-oriented `NSTD-1` and `UCIO-1` bitgen blockers to warnings just before `write_bitstream`. `drc.rpt` keeps their original severities. Board projects should still constrain every pin.

A regression manifest lists `fpga.yaml` suites:

```yaml
rtl-buddy-filetype: fpga_reg_config

fpga-configs:
  - blocks/counter/fpga.yaml
  - blocks/fifo/fpga.yaml
```

Runs above `-l/--reg-level` are SKIP. Machine-mode regression results include the originating suite. Selection and output options are in the [CLI reference](../reference/cli.md).

Filelist `+incdir+` entries reach both backends: Vivado's `synth_design` gets them as `-include_dirs` and openXC7's `read_verilog` as `-I`. Each directory resolves against the filelist that declared it.

## Read timing and quality results

Use machine mode for automation:

```bash
rb --machine fpga demo_fpga > result.json
```

Vivado results can include LUT, FF, BRAM and DSP utilization; WNS, TNS and hold slack; timing status and failing paths; power; DRC counts; methodology warnings; and the bitstream path. openXC7 emits the smaller set described above.

To close timing:

1. Read `timing_met`, `wns_ns` and the worst `failing_paths`.
2. Compare `requirement_ns`, endpoints, logic depth and routing delay where available.
3. Change one constraint, RTL pipeline stage, placement choice or tool directive.
4. Rerun the same command and compare WNS.
5. Stop when timing closes or the change no longer helps.

Choose the change from the path evidence:

- An unrealistic requirement points to a wrong `create_clock`.
- A valid cross-domain or quasi-static path may need a false-path or multicycle exception. Confirm the functional relationship first; do not add exceptions just to silence a path.
- Logic-dominated delay suggests pipelining.
- Routing-dominated delay suggests congestion or placement work.

Methodology warnings are informational and do not change pass/fail.

## Read power results

Vivado runs `report_power` after routing and reports total, dynamic and static watts. These are vectorless estimates, suitable for comparing runs but not for signoff. Check the confidence and activity assumptions in `power.rpt` before treating them as absolute. openXC7 reports no power.

## Generate or audit CDC constraints

Generate CDC timing exceptions from an analyzed crossing set, or audit an existing XDC:

```bash
rb cdc <name> --emit-constraints --format xdc -o constraints/cdc.xdc
rb cdc <name> --check-xdc constraints/board.xdc
```

Add generated constraints to the run's `xdc` list. `--check-xdc` audits CDC exceptions only; Vivado still validates pins, placement and electrical rules.

Xilinx XPM CDC macros need an `rtl-buddy-cdc` that recognizes the XPM family. Register other known synchronizer primitives with the CDC tool's `--sync-primitive MODULE` extra argument. Use the XDC audit's recognition override only when the engine cannot model a legitimate custom macro.

## Find artefacts

Each run writes `<fpga.yaml directory>/artefacts/<run>/`.

- **Vivado**: `fpga.f`, `flow.tcl`, `vivado.log`, utilization, timing, power, DRC and methodology reports, and optionally `<top>.bit`.
- **openXC7**: `fpga.f`, `synth.ys`, `yosys.log`, `<top>.json`, `nextpnr.log`, `<top>.fasm`, and optionally prjxray stage logs and `<top>.bit`.

Both backends delete their outputs before each run: reports, netlist, FASM and frames handed between stages, and the bitstream. A run that fails partway therefore leaves absent what it never wrote, not the previous run's copy. Logs are truncated by the stage that writes them.

A run without `--bitstream` removes any previously built `<top>.bit`, so a stale deployable bitstream never sits beside a run that reports none. Rerun with `--bitstream` to regenerate it, or copy the file out first.

A run whose backend tool is not installed deletes nothing. A configuration error is not a skip: an unknown `platform:` or a part the backend cannot build is reported whether or not the toolchain is present, and clears the outputs.

Do not give an FPGA run and a power run the same name within one suite. Both own `artefacts/<name>/power.rpt`, and the second to run overwrites the first. Names shared with a CDC analysis or a simulation test do not clear each other's outputs, but a directory is easier to read when one run owns it.

## Interpret pass, fail, and skip

A run passes when every backend stage exits zero, the logs contain no backend error records, required reports parse, and a requested bitstream exists. Otherwise it fails and names the failing stage or output.

A timing miss alone fails the run only with `require-timing-met: true`. Missing backend tools or data, licensing detected as unavailable during setup, and regression-level filtering return SKIP.

If a run fails:

1. Read the returned description.
2. Inspect `vivado.log` or the named openXC7 stage log.
3. Confirm executable and data paths with `rb tool-check`.
4. Fix configuration or tool errors before interpreting incomplete metrics.
