---
description: Discover AXI bundles, generate a simulation monitor, profile a trace, and inspect transactions with the AXI profiling workflow.
---

# AXI Interconnect Profiling

`rb axi-profile` turns a simulation trace into aggregate AXI performance metrics and optional per-transaction Parquet data. It wraps the standalone `axi-profiler` executable and supports AXI4, AXI4-Lite and AXI4-Stream bundles only.

## Install the profiler

The base tool covers discovery, monitor generation and trace ingestion:

```bash
uv tool install rtl-buddy-axi-profiler
```

Add extras for optional outputs: `parquet` for transaction output, `notebook` for interactive analysis. Install one form, or the combined one for both:

```bash
uv tool install 'rtl-buddy-axi-profiler[parquet]'
uv tool install 'rtl-buddy-axi-profiler[notebook]'
uv tool install 'rtl-buddy-axi-profiler[parquet,notebook]'
```

Add `--force` to replace an existing tool environment. Pass `--tool /path/to/axi-profiler` to use a specific executable. See [Installation](../install.md#external-tools-by-feature) for external-tool setup.

## Configure model outputs

Point the model at a checked-in bundle manifest and a generated monitor file:

```yaml
models:
  - name: soc_top
    filelist: [-F soc_top.f]
    axi_bundles: axi-bundles.yaml
    axi_monitor_out: ../verif/soc_top/gen/axi_perf_mon.sv
```

Paths are relative to `models.yaml`. `discover` writes `axi_bundles`; `gen-monitor` and `run` read it. `gen-monitor` writes `axi_monitor_out`. Put that file in the verification tree and add it to the testbench filelist once.

Both fields are optional until a command needs them. A command with a missing required field fails before the external tool runs and names the step that produces it.

## Run the profiling pipeline

The four stages run in this order. Discovery and monitor generation take a model; trace ingestion and notebook launch take a test.

```bash
rb axi-profile discover soc_top
rb axi-profile gen-monitor soc_top --time-precision 1ps
rb test my_test
rb axi-profile run my_test --emit-txns-parquet
rb axi-profile notebook my_test
```

## Discover AXI bundles

```bash
rb axi-profile discover soc_top
rb axi-profile discover soc_top -c design/soc_top/models.yaml
rb axi-profile discover soc_top -o /tmp/axi-bundles.yaml
```

rtl_buddy builds a stripped, deduplicated model filelist and asks the profiler to discover bundles. Output goes to `-o`, else the model's `axi_bundles` path, else `artefacts/axi/<model>/axi-bundles.yaml`.

Commit the manifest so RTL-interface changes are reviewable. Discovery rewrites it in full, and `--amend` does not preserve manual edits.

## Generate and compile the monitor

```bash
rb axi-profile gen-monitor soc_top
rb axi-profile gen-monitor soc_top -o /tmp/axi_perf_mon.sv
rb axi-profile gen-monitor soc_top --time-precision 1ps --buffer-cap 16384
```

The generated SystemVerilog attaches with `bind`, so the RTL is not modified. Add the file to the testbench filelist before simulating.

- `--time-precision` must equal the wrapping testbench's IEEE 1800 `timeprecision`; a mismatch scales timestamps wrongly.
- `--buffer-cap` bounds each bundle's in-memory FIFO.
- The monitor drains its buffers only at `$finish`, so the simulation must exit normally.

## Profile a test trace

```bash
rb axi-profile run my_test
rb axi-profile run my_test --emit-txns-parquet
rb axi-profile run my_test --emit-txns-parquet-path /tmp/txns.parquet
rb axi-profile run my_test --tb-prefix my_custom_wrapper
```

The test supplies its model, manifest, testbench scope and newest trace. Outputs:

- `artefacts/axi/<test>/axi-perf.json`: aggregate throughput and latency per bundle.
- `artefacts/axi/<test>/axi-txns.parquet`: per-transaction data, only when enabled. An explicit `--emit-txns-parquet-path` enables it.

Use `--tb-prefix` when the simulator wrapper renames the testbench scope; an empty value disables prefix matching.

The newest supported trace in `<suite>/artefacts/<test>/` is used:

| Trace | Handling |
| --- | --- |
| `dump.fst` | Read directly. |
| `dump.vcd` | Read directly. |
| `vcdplus.vpd` | Convert with `vpd2vcd`, then `vcd2fst` when available. |

VPD conversion logs to `vpd-convert.log` and caches `vcdplus.fst` beside the input; a cache newer than the VPD is reused. Without `vcd2fst`, the larger temporary VCD is kept and read directly. `vpd2vcd` comes with the VCS installation that produced the VPD, and `vcd2fst` with GTKWave.

## Open the transaction notebook

```bash
rb axi-profile notebook my_test
rb axi-profile notebook my_test --port 2718
rb axi-profile notebook my_test --headless
```

The notebook needs the per-test Parquet file from `run --emit-txns-parquet` and a `marimo` executable. A missing input fails with the command or extra that creates it.

The default mode runs in the foreground with the packaged notebook template. `--headless` disables the marimo token so the loopback-only hub can open the URL. `--daemon` is accepted but still runs in the foreground; use a hub-launched notebook when the caller must return immediately.

## Find artefacts and logs

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

Files exist only for stages that ran, and a custom `-o` path replaces the matching default.

Each subcommand returns the profiler's exit code. For elaboration, ingest or write failures, read the matching log. Configuration errors and missing manifest, trace or notebook prerequisites are reported before the tool is invoked.

## Hub integration

Add the aggregate result to generated schematics:

```bash
rb hub start --serve-viewer \
  --axi-perf-from <suite>/artefacts/axi/<test>/axi-perf.json
```

Use the per-test path above so the hub can infer the test and suite. The schematic then shows performance badges and can launch the matching marimo notebook. A hub-launched notebook joins the local event broker, so bundle selections in the schematic update the notebook.

The overlay is a hub view-builder option, not an `rb hier` option. See [Hub](hub.md#axi-perf-overlay-and-notebook-spawning).
