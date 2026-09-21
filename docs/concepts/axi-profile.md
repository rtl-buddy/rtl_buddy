---
description: Discover AXI bundles, generate a simulation monitor, profile a trace, and inspect transactions with the AXI profiling workflow.
---

# AXI Interconnect Profiling

`rb axi-profile` turns a simulation trace into aggregate AXI performance metrics and optional per-transaction Parquet data. The workflow uses the standalone `axi-profiler` executable and supports AXI4, AXI4-Lite, and AXI4-Stream bundles.

## Install the profiler

Install the base tool for discovery, monitor generation, and trace ingestion:

```bash
uv tool install rtl-buddy-axi-profiler
```

Include optional outputs when installing the tool:

```bash
uv tool install 'rtl-buddy-axi-profiler[parquet]'
uv tool install 'rtl-buddy-axi-profiler[notebook]'
uv tool install 'rtl-buddy-axi-profiler[parquet,notebook]'
```

Choose one command: `parquet` provides transaction output, `notebook` provides interactive analysis, and the combined form enables both. Add `--force` when replacing an existing tool environment. Pass `--tool /path/to/axi-profiler` to use a specific executable. See [Installation](../install.md#external-tools-by-feature) for external-tool setup.

## Configure model outputs

Point the model at a checked-in bundle manifest and generated monitor:

```yaml
models:
  - name: soc_top
    filelist: [-F soc_top.f]
    axi_bundles: axi-bundles.yaml
    axi_monitor_out: ../verif/soc_top/gen/axi_perf_mon.sv
```

Paths are relative to `models.yaml`. `axi_bundles` is written by `discover` and read by `gen-monitor` and `run`. `axi_monitor_out` is written by `gen-monitor`; place it in the verification tree and add it to the testbench filelist once.

Both fields are optional until a command needs them. A missing required field fails before the external tool runs and points to the prerequisite step.

## Run the profiling pipeline

Run the four stages in order:

```bash
rb axi-profile discover soc_top
rb axi-profile gen-monitor soc_top --time-precision 1ps
rb test my_test
rb axi-profile run my_test --emit-txns-parquet
rb axi-profile notebook my_test
```

The stages are independent wrappers around the external profiler. Discovery and monitor generation select a model; trace ingestion and notebook launch select a test.

## Discover AXI bundles

```bash
rb axi-profile discover soc_top
rb axi-profile discover soc_top -c design/soc_top/models.yaml
rb axi-profile discover soc_top -o /tmp/axi-bundles.yaml
```

rtl_buddy generates a stripped, deduplicated model filelist, then asks the profiler to discover bundles. Without `-o`, output goes to the model's `axi_bundles` path or, when unset, `artefacts/axi/<model>/axi-bundles.yaml`.

Commit the manifest so RTL-interface changes are reviewable. Discovery rewrites it in full; `--amend` does not merge prior manual edits.

## Generate and compile the monitor

```bash
rb axi-profile gen-monitor soc_top
rb axi-profile gen-monitor soc_top -o /tmp/axi_perf_mon.sv
rb axi-profile gen-monitor soc_top --time-precision 1ps --buffer-cap 16384
```

The generated SystemVerilog uses `bind` so it can observe the DUT without modifying RTL. Add the generated file to the testbench filelist before simulation.

`--time-precision` must match the wrapping testbench's IEEE 1800 `timeprecision`; a mismatch scales timestamps incorrectly. `--buffer-cap` bounds each bundle's in-memory FIFO. The monitor drains its buffers only at `$finish`, so ensure the simulation exits normally.

## Profile a test trace

```bash
rb axi-profile run my_test
rb axi-profile run my_test --emit-txns-parquet
rb axi-profile run my_test --emit-txns-parquet-path /tmp/txns.parquet
rb axi-profile run my_test --tb-prefix my_custom_wrapper
```

The test resolves its model, manifest, testbench scope, and newest trace. The default outputs are:

- `artefacts/axi/<test>/axi-perf.json` for aggregate bundle throughput and latency.
- `artefacts/axi/<test>/axi-txns.parquet` when transaction output is enabled.

An explicit Parquet path enables transaction output automatically. Use `--tb-prefix` when the simulator wrapper renames the testbench scope; pass an empty value to disable prefix matching.

The newest supported trace in `<suite>/artefacts/<test>/` wins:

| Trace | Handling |
| --- | --- |
| `dump.fst` | Read directly. |
| `dump.vcd` | Read directly. |
| `vcdplus.vpd` | Convert with `vpd2vcd`; use `vcd2fst` when available. |

VPD conversion writes `vpd-convert.log` and caches `vcdplus.fst` beside the input. A cache newer than the VPD is reused. Without `vcd2fst`, the larger temporary VCD is retained and read directly. A VCS installation that produced VPD normally supplies `vpd2vcd`; GTKWave supplies `vcd2fst`.

## Open the transaction notebook

```bash
rb axi-profile notebook my_test
rb axi-profile notebook my_test --port 2718
rb axi-profile notebook my_test --headless
```

The notebook requires the canonical per-test Parquet file from `run --emit-txns-parquet` and a `marimo` executable. Missing inputs fail with the exact command or extra needed to create them.

Foreground mode opens the packaged notebook template. `--headless` disables the marimo token so the loopback-only hub can open the URL. `--daemon` is accepted but runs in the foreground; use hub-managed launch when the caller must return immediately.

## Find artefacts and logs

Outputs are under `artefacts/axi/`:

```text
artefacts/axi/
├── <model>/
│   ├── axi.f
│   ├── axi-bundles.yaml
│   ├── axi-profile-discover.log
│   └── axi-profile-gen-monitor.log
└── <test>/
    ├── axi.f
    ├── axi-perf.json
    ├── axi-txns.parquet
    ├── axi-profile-run.log
    └── axi-profile-notebook.log
```

Files appear only for stages that produce them; a custom `-o` path replaces the corresponding default output.

Each subcommand returns the external profiler's exit code. For elaboration, ingest, or write failures, inspect the matching log. Configuration, missing manifest, missing trace, and missing notebook prerequisites are reported before invoking the tool.

## Hub integration

Add the aggregate result to generated schematics:

```bash
rb hub start --serve-viewer \
  --axi-perf-from <suite>/artefacts/axi/<test>/axi-perf.json
```

Use the canonical per-test location so the hub can infer the test and suite. The schematic shows performance badges and can launch the matching marimo notebook. A hub-launched notebook joins the local event broker, allowing schematic bundle selections to update the notebook.

The AXI overlay is a hub view-builder option, not an `rb hier` option. See [Hub](hub.md#axi-perf-overlay-and-notebook-spawning).

## Current constraints

- Only AXI4, AXI4-Lite, and AXI4-Stream are supported.
- Notebook launch is foreground unless managed by the hub.
- Discovery rewrites the manifest; it does not preserve manual edits.
