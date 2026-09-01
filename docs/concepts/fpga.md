---
description: Configure and run FPGA synthesis, placement, routing, reports, and optional bitstream generation with Vivado or openXC7.
---

# FPGA Implementation

`rb fpga` implements a model for one FPGA part, writes post-route reports, and returns structured utilization and timing metrics. Add `--bitstream` only when you need a programming file.

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

## Configure `fpga.yaml`

Each run references a model and either a part or a reusable platform:

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

`model_path` and XDC paths are relative to `fpga.yaml`. The synthesis top is the model's root module — its `top:` in `models.yaml`, defaulting to the model name — and it also names the emitted bitstream (`<top>.bit`). `part` and `platform` are mutually exclusive; naming both is an exit-2 configuration error. Expected-failure fields follow [Expected Failures](expected-failures.md).

`require-timing-met` defaults to false. A completed route that misses timing therefore passes the flow while reporting `timing_met: false`, negative slack, and failing paths. Set it true when timing closure is a regression gate; the failing result still includes timing metrics. A backend that reports no timing result is not failed by this option.

## Platforms: `cfg-fpga-platforms`

Define shared part and board constraints in `cfg-fpga-platforms`, then select the platform from a run:

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

Platform XDC files are read first. Per-run XDC files extend that set and are read afterward, so later run-level commands can override platform defaults. Platform paths resolve from `root_config.yaml`; run paths resolve from `fpga.yaml`.

## Run one suite or a regression

```bash
rb fpga
rb fpga demo_fpga -c fpga/demo/fpga.yaml
rb fpga demo_fpga --bitstream
rb fpga --list
rb fpga-regression -c ci/fpga_regression.yaml -l 1000
```

Without `--bitstream`, the flow stops after routing and reports; `bitstream` is `null`. With bitstream generation enabled, Vivado downgrades the IP-oriented `NSTD-1` and `UCIO-1` bitgen blockers to warnings immediately before `write_bitstream`. Their original severities remain in `drc.rpt`; board projects should still constrain every pin.

A regression manifest lists `fpga.yaml` suites:

```yaml
rtl-buddy-filetype: fpga_reg_config

fpga-configs:
  - blocks/counter/fpga.yaml
  - blocks/fifo/fpga.yaml
```

Runs above `-l/--reg-level` are SKIP. Machine-mode regression results include the originating suite. See the [CLI reference](../reference/cli.md) for selection and output options.

## Read timing and quality results

Use machine mode for automation:

```bash
rb --machine fpga demo_fpga > result.json
```

Vivado results may include LUT, FF, BRAM, and DSP utilization; WNS, TNS, and hold slack; timing status and failing paths; power; DRC counts; methodology warnings; and bitstream path. openXC7 emits its smaller metric set described above.

For timing closure:

1. Read `timing_met`, `wns_ns`, and the worst `failing_paths`.
2. Compare `requirement_ns`, endpoints, logic depth, and routing delay where available.
3. Change one relevant constraint, RTL pipeline, placement choice, or tool directive.
4. Rerun the same command and compare WNS.
5. Stop when timing closes or the chosen change no longer improves the result.

Use path evidence to select the change:

- An unrealistic requirement suggests correcting `create_clock`.
- A valid cross-domain or quasi-static path may need a false- or multicycle-path exception.
- Logic-dominated delay suggests pipelining.
- Routing-dominated delay suggests congestion or placement work.

Do not add timing exceptions merely to silence a path; confirm the functional relationship first. Post-route vectorless power is suitable for comparing runs, not signoff. Methodology warnings remain informational and do not change pass/fail.

## Power

Vivado runs `report_power` after routing and reports total, dynamic, and static watts. These vectorless estimates are useful for comparing runs; use the confidence and activity assumptions in `power.rpt` before treating them as absolute values. openXC7 does not report power.

## Generate or audit CDC constraints

Generate CDC timing exceptions from an analyzed crossing set, or check an existing XDC:

```bash
rb cdc <name> --emit-constraints --format xdc -o constraints/cdc.xdc
rb cdc <name> --check-xdc constraints/board.xdc
```

Add generated constraints to the run's `xdc` list. `--check-xdc` audits CDC exceptions only; Vivado remains responsible for pin, placement, and electrical validation.

Xilinx XPM CDC macros require a compatible `rtl-buddy-cdc` that recognizes the XPM family. Register other known synchronizer primitives through the CDC tool's `--sync-primitive MODULE` extra argument. Use the XDC audit's recognition override only when the engine cannot model a legitimate custom macro.

## Find artefacts

Each run writes `<fpga.yaml directory>/artefacts/<run>/`.

Vivado produces `fpga.f`, `flow.tcl`, `vivado.log`, utilization/timing/power/DRC/methodology reports, and optionally `<top>.bit`.

openXC7 produces `fpga.f`, `synth.ys`, `yosys.log`, `<top>.json`, `nextpnr.log`, `<top>.fasm`, and optional prjxray stage logs plus `<top>.bit`.

Both backends delete their outputs before each run — the reports, the netlist, FASM and frames handed between stages, and the bitstream — so a run that fails partway leaves what it never wrote absent instead of the previous run's copy. The logs are exempt: each is truncated by the stage that writes it.

The bitstream goes even when the run was not asked to build one: a run without `--bitstream` removes a previously built `<top>.bit`, because the artefact directory describes the latest run and a stale deployable bitstream sitting beside a run that reports none is exactly the trap the rest of this rule closes. Rerun with `--bitstream` to regenerate it, or copy the file out first.

A run that cannot find its backend tool is the exception: it deletes nothing, because a machine without the tool never ran it and has no business removing what a machine that has it produced. A configuration error is not a skip — an unknown `platform:`, or a part the backend cannot build, is reported whether or not the toolchain is present, and clears the outputs on its way out.

Name FPGA runs distinctly from other commands' entries. An FPGA run and a power run must not share a name within one suite: both own `artefacts/<name>/power.rpt` and the second to run overwrites the first. Ownership cannot be told apart by filename, so rtl_buddy does not try — give them distinct names. Names that collide with a CDC analysis or a simulation test are safe for artifact clearing — those outputs are protected — but a shared directory is still easier to read when one run owns it.

## Interpret pass, fail, and skip

A run passes when every backend stage exits zero, logs contain no backend error records, required reports parse, and a requested bitstream exists. It fails otherwise and names the failing stage or output.

Timing failure alone does not fail the run unless `require-timing-met: true`. Missing backend tools or data, licensing unavailability detected as setup, and regression-level filtering return SKIP.

If a run fails:

1. Read the returned description.
2. Inspect `vivado.log` or the named openXC7 stage log.
3. Confirm the executable and data paths with `rb tool-check`.
4. Fix configuration or tool errors before interpreting incomplete metrics.

Current limitation: include-directory (`+incdir+`) entries are not propagated into the generated Vivado or openXC7 synthesis command.
