---
description: How to profile AXI interconnect performance with rtl_buddy via the rb axi-profile subcommand group, models.yaml fields, and the standalone rtl-buddy-axi-profiler.
---

# AXI Interconnect Profiling

> **Integration type:** Pluggable — curated. `rb axi-profile` drives the standalone [rtl-buddy-axi-profiler](https://github.com/rtl-buddy/rtl-buddy-axi-profiler) at subprocess granularity; rtl_buddy is not coupled to its Python API.
>
> **External binary required:** `axi-profiler` — install with `uv tool install rtl-buddy-axi-profiler`.
>
> **Optional extras:** `[parquet]` (pyarrow) for `--emit-txns-parquet`; `[notebook]` (marimo + altair + polars) for `rb axi-profile notebook`.
>
> See also: [Installation — External tools by feature](../install.md#external-tools-by-feature).

`rb axi-profile` is a workflow for measuring AXI interconnect performance directly from a simulation trace. The four subcommands form a pipeline:

1. **`discover`** — parse RTL to produce an `axi-bundles.yaml` manifest of the model's AXI interfaces.
2. **`gen-monitor`** — emit a bind-style SystemVerilog monitor whose `axi-stream` taps are added to the testbench's filelist.
3. **`run`** — ingest a test's FST trace and emit per-test `axi-perf.json` (and optionally a per-transaction Parquet).
4. **`notebook`** — launch the packaged marimo notebook against the per-transaction Parquet for interactive deep-dive.

The four wrappers share the same subprocess-granularity integration: pass `--tool /path/to/axi-profiler` to pin a specific build, otherwise the binary on `PATH` is used.

## Installing rtl-buddy-axi-profiler

```bash
uv tool install rtl-buddy-axi-profiler                    # base — discover, gen-monitor, run
uv pip install 'rtl-buddy-axi-profiler[parquet]'          # adds pyarrow for --emit-txns-parquet
uv pip install 'rtl-buddy-axi-profiler[notebook]'         # adds marimo + altair + polars for rb axi-profile notebook
```

The base install gives you `discover`, `gen-monitor`, and `run` without parquet. The `[parquet]` extra unlocks `--emit-txns-parquet`, which is the prerequisite for `rb axi-profile notebook`. The `[notebook]` extra additionally brings in marimo so the notebook can be launched.

## `models.yaml` fields

Two optional fields on each `models.yaml` entry drive `rb axi-profile`:

```yaml
models:
  - name: "soc_top"
    filelist:
      - "-F soc_top.f"
    axi_bundles: "axi-bundles.yaml"               # manifest path (input to run / gen-monitor)
    axi_monitor_out: "../verif/soc_top/gen/axi_perf_mon.sv"  # where gen-monitor writes
```

| Field | Description |
|-------|-------------|
| `axi_bundles` | Relative path from `models.yaml` to the model's checked-in `axi-bundles.yaml` manifest. Consumed by `rb axi-profile run` and `rb axi-profile gen-monitor`; produced by `rb axi-profile discover`. |
| `axi_monitor_out` | Relative path from `models.yaml` to where `rb axi-profile gen-monitor` writes the generated SV monitor. Typically points into the verif testbench source tree so the file is picked up by the tb's filelist (e.g. `../verif/soc_top/gen/axi_perf_mon.sv`). |

Both fields are optional from rtl_buddy's perspective; missing them surfaces a clear error from the subcommand that needs them, pointing at the prerequisite command.

## Subcommand: `discover`

```bash
# Generate axi-bundles.yaml at the path declared in models.yaml
rb axi-profile discover soc_top

# Custom output path
rb axi-profile discover soc_top -o /tmp/axi-bundles.yaml

# Different models.yaml
rb axi-profile discover soc_top -c design/soc_top/models.yaml
```

The runner writes a stripped, deduplicated filelist for the model, then invokes `axi-profiler discover --top <model> --filelist <fl> --output <path>`. When `-o` is omitted, the output goes to the model's `axi_bundles:` path if set, otherwise to `artefacts/axi/<model>/axi-bundles.yaml`.

The generated `axi-bundles.yaml` is a checked-in manifest — re-running `discover` after RTL changes lets you diff the manifest in code review. The `--amend` option is reserved for a future user-edit merge workflow; passing it today emits a warning.

## Subcommand: `gen-monitor`

```bash
# Emit SV monitor at model.axi_monitor_out
rb axi-profile gen-monitor soc_top

# Custom output path
rb axi-profile gen-monitor soc_top -o /tmp/axi_perf_mon.sv

# Match the testbench's `timeprecision`
rb axi-profile gen-monitor soc_top --time-precision 1ps

# Cap per-bundle FIFO depth (drained only at $finish)
rb axi-profile gen-monitor soc_top --buffer-cap 16384
```

The runner reads the manifest from `model.axi_bundles` and invokes `axi-profiler gen-monitor <manifest> --output <path>`. The generated `.sv` file uses SystemVerilog `bind` semantics so the monitor instances live alongside the DUT without modifying its source.

You add the generated SV to the testbench's filelist once. If `axi_monitor_out:` points at a path inside the verif tree (e.g. `../verif/soc_top/gen/axi_perf_mon.sv`), that's a one-time step — subsequent `gen-monitor` runs just rewrite the file in place.

`--time-precision` must match the IEEE 1800 `timeprecision` of the wrapping testbench, otherwise the monitor's timestamp arithmetic will be off by a power of ten. `--buffer-cap` bounds memory growth on extremely long traces — the buffer is drained to disk only at `$finish`.

## Subcommand: `run`

```bash
# Ingest the test's FST and emit axi-perf.json
rb axi-profile run my_test

# Also produce the per-txn parquet that the notebook reads
rb axi-profile run my_test --emit-txns-parquet

# Explicit parquet path (implies --emit-txns-parquet)
rb axi-profile run my_test --emit-txns-parquet-path /tmp/txns.parquet

# Custom output path for axi-perf.json
rb axi-profile run my_test -o /tmp/axi-perf.json

# Override the FST top scope (default = the test's tb name)
rb axi-profile run my_test --tb-prefix my_custom_wrapper
```

The runner resolves everything from `tests.yaml` and the standard artefact layout:

| Input | Where it comes from |
|-------|---------------------|
| Model | `tests.yaml` → `model:` |
| Manifest | `model.axi_bundles` in `models.yaml` (must exist — run `discover` first) |
| FST trace | `<suite_dir>/artefacts/<test>/dump.fst` (same convention as `rb wave`) |
| Top scope prefix | The test's `testbenches:` entry name in `tests.yaml` |

You only type `rb axi-profile run <test>` — everything else auto-resolves. The `--tb-prefix` override exists for setups where the Verilator wrapper renames the testbench scope; pass an empty string to disable prefix matching entirely.

Pass `--emit-txns-parquet` to also write per-transaction rows to `artefacts/axi/<test>/axi-txns.parquet` — that's the canonical location `rb axi-profile notebook` reads. Requires the `axi-profiler` `[parquet]` extra (pyarrow).

## Subcommand: `notebook`

```bash
# Foreground (default) — opens marimo in your browser
rb axi-profile notebook my_test

# Pin the marimo edit-server port
rb axi-profile notebook my_test --port 2718

# Hub-initiated (SPA opens the URL; marimo runs headless without a token)
rb axi-profile notebook my_test --headless
```

The runner resolves three things up-front:

1. The per-test parquet at `artefacts/axi/<test>/axi-txns.parquet` — missing → clear error pointing at `rb axi-profile run <test> --emit-txns-parquet`.
2. The notebook template via `importlib.resources.files('rtl_buddy_axi_profiler.notebook') / 'template.py'`.
3. The `marimo` binary on `PATH` — missing → install hint for `rtl-buddy-axi-profiler[notebook]`.

It then spawns `marimo edit <template>` with `$AXI_TXNS_PARQUET` exported so the template's first cell loads the parquet. `--headless` adds `--no-token` so the SPA can navigate to the URL without threading a per-session token through the hub→browser handoff — loopback-only, so the security trade is fine.

`--daemon` is accepted but currently falls back to foreground; background detach is a follow-up (same pattern as `rb hub start`).

## Artefacts

Per-model and per-test outputs land under `artefacts/axi/`:

```
artefacts/axi/
├── <model>/
│   ├── axi.f                                    # filelist used by discover / gen-monitor
│   ├── axi-bundles.yaml                         # only when -o defaults here
│   ├── axi-profile-discover.log                 # stderr from `axi-profiler discover`
│   └── axi-profile-gen-monitor.log              # stderr from `axi-profiler gen-monitor`
└── <test>/
    ├── axi.f                                    # filelist used by run
    ├── axi-perf.json                            # aggregate per-bundle throughput / latency
    ├── axi-txns.parquet                         # per-transaction rows (only with --emit-txns-parquet)
    ├── axi-profile-run.log                      # stderr from `axi-profiler run`
    └── axi-profile-notebook.log                 # stderr from marimo
```

`axi-perf.json` is the artefact picked up by `rb hier --overlay axi-perf=...` and by the hub's view-builder when `--axi-perf-from` is wired up — see [Hub](hub.md) for the SPA overlay flow.

## Hub integration

When the [coordination hub](hub.md) is running, two paths surface AXI-perf data in the rtl-buddy-view SPA:

- **Static overlay**: `rb hub start --axi-perf-from <test>` threads the test's `axi-perf.json` into the SPA's view builder, decorating each AXI bundle node with throughput badges.
- **Notebook launch**: the SPA's "Open in marimo" button calls `/api/axi-profile/notebook?test=<name>`, which invokes `rb axi-profile notebook <test> --headless` and proxies the marimo URL back to the SPA. The user gets the full interactive notebook without leaving the hub UI.

Both flows reuse the same per-test artefact layout, so the static and interactive views agree on what data they're showing.

## Pass/fail detection

Each `rb axi-profile` subcommand exits with the underlying `axi-profiler` exit code. A non-zero exit means the profiler reported an elaboration, ingest, or write error — check the relevant `.log` file under `artefacts/axi/`.

Missing prerequisites (no `axi_bundles:` in `models.yaml`, no FST at the expected path, no `marimo` for `notebook`) surface as a clear `FatalRtlBuddyError` *before* invoking `axi-profiler`, so the error is anchored at the prerequisite step rather than buried in profiler output.

## Out of scope (today)

- **Non-AXI protocols.** AXI4 / AXI4-Lite / AXI4-Stream are supported by `rtl-buddy-axi-profiler`'s bundle discovery; AHB, APB, TileLink, and custom protocols are not. The pluggable wrapper boundary makes adding sibling profilers straightforward, but no other profilers are wired up yet.
- **Background-detached `notebook`.** `--daemon` is accepted for forward compatibility but runs in foreground today.
- **Manifest user-edit merging.** `rb axi-profile discover --amend <prev>` is reserved for merging user edits across re-runs; today the manifest is rewritten in full and you diff in git.
