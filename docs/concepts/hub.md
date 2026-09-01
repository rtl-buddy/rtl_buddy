---
description: Start and operate the rtl_buddy hub, connect browser and editor peers, switch designs, send commands, and diagnose connection or view failures.
---

# Hub (`rb hub`)

The hub coordinates the schematic, waveform viewer, source editor, graph pane, and coverage pane. It translates between their view, wave, and source coordinates and routes live events among connected peers.

## Quick start

Start the hub from the project root:

```bash
uv run rb hub start --serve-viewer
```

Open the printed `http://127.0.0.1:<http_port>/` URL. The landing page links the available apps:

| Route | App |
| --- | --- |
| `/sch` | Interactive schematic. |
| `/gph` | Design knowledge graph. |
| `/cov` | Coverage browser. |

Use a second shell to inspect or stop the process:

```bash
uv run rb hub status
uv run rb hub log --follow
uv run rb hub stop
```

`rb hub start` stays in the foreground by default. Add `--daemon` to detach and log to `.rtl-buddy/hub.log`. Startup waits until the detached process publishes discovery; an early failure returns non-zero with the log tail.

The hub itself has no external binary dependency. The schematic needs `rtl-buddy-sch`, and live wave integration needs the rtl-buddy Surfer fork. See [Installation](../install.md#external-tools-by-feature) and [Waveform Viewer](wave.md).

## Start with a model or testbench

Generate and serve a schematic from `models.yaml` at startup:

```bash
rb hub start --serve-viewer --model ip_demo_tiny_npu
rb hub start --serve-viewer --model ip_demo_tiny_npu \
  --models-file design/npu/models.yaml
```

`--model` requires `--serve-viewer`. Without `--models-file`, the hub searches the project and requires exactly one matching model. Zero or multiple matches fail with the discovered files and model names. Use `--models-file` to constrain discovery when names overlap.

The browser can switch without restarting:

- `GET /models` lists models and their current view status.
- `GET /view.json?model=NAME` builds or reuses `.rtl-buddy/cache/view-<NAME>.json`, activates it, and broadcasts `view_changed`.
- `GET /tests` lists runnable testbench views.
- `GET /view.json?test=NAME` builds and activates a TB-rooted view from the test's model and testbench.

Model discovery is refreshed per request. View generation is serialized per model or test to prevent duplicate concurrent builds. Restart the hub when you need to force regeneration after source changes.

A rebuild clears the cached `view-<NAME>.json` and the model's cached domain map before it starts, so a build that fails — a bad `cdc:` back-pointer, a missing viewer, a crashed analyzer — reports the failure instead of leaving the previous build's hierarchy to be served in its place. A renderer that writes the file and *then* fails, or writes one whose schema this rtl_buddy rejects, has it removed too: the hub serves a cached view by testing the file, so a rejected view left on disk would be served regardless of the recorded failure.

## Diagnose view errors

Failed `GET /view.json` requests return JSON with `error.kind`. Branch on the kind, not the prose:

| Kind | Meaning | Recovery |
| --- | --- | --- |
| `view_generation_failed` | Filelist, parse, or elaboration failed. | Read `log_tail` or `log_path`, fix the model, then restart or request it again. |
| `unknown_model` | No unique matching model exists. | Correct the name or pass `--models-file`. |
| `no_active_model` | No model or prebuilt view is selected. | Request `?model=NAME` or start with `--model`. |
| `no_project_root` | The hub cannot discover project configuration. | Start inside the project or provide the correct root context. |

`view_generation_failed` includes the final renderer log lines. Common causes are unsupported parser syntax, missing submodules, and filelist entries the renderer cannot consume.

## Discovery and configuration

The hub writes `.rtl-buddy/hub.json` after binding. It contains the PID, TCP address, project root, server version, and optional HTTP port and active model. Peers discover this file by walking upward from their current directory.

Set `RTL_BUDDY_HUB=<host>:<port>` when a peer runs outside the project tree. Use the `tcp` value from `hub.json`; the variable is not a file path.

Optional `.rtl-buddy/hub.toml` settings include:

```toml
[hub]
listen_port = 0
http_port = 0
log_path = ".rtl-buddy/hub.log"

[mapping]
tb_prefix = "tb.dut."
view_json = ".rtl-buddy/view.json"

[[mapping.signal_aliases]]
wave = "tb.legacy_dut.clk"
view = "tb.dut.clk"
```

Port `0` lets the OS choose. Relative paths resolve from the project root. Signal aliases are applied before `tb_prefix` is removed. Validate edits with:

```bash
rb hub config validate
```

Only `[hub]` and `[mapping]` are valid top-level sections. Unknown keys inside those sections are tolerated for forward compatibility.

## Connect peers

The hub accepts inbound connections only; every adapter is responsible for connecting and reconnecting.

| Peer | Origin | Transport |
| --- | --- | --- |
| Schematic SPA | `view` | WebSocket `/ws`. |
| Graph pane | `graph` | WebSocket `/ws`. |
| Coverage pane | `cov` | WebSocket `/ws`. |
| `rb wave` bridge | `wave` | Line-delimited JSON over TCP. |
| Editor adapter | `src` | Line-delimited JSON over TCP. |
| `rb hub send` | `cli` | One-shot TCP client. |

The hub permits one client per origin. A second browser tab can take over and disconnect the prior tab; the prior tab stops reconnecting until the user explicitly takes the connection back. The landing page does not register an origin and therefore cannot evict an app.

`rb hub status` shows the live origins. It intentionally reports protocol origin names such as `view` and `graph`, while the browser labels those apps `sch` and `gph`.

## Driving the hub from the CLI

`rb hub send` is the scripting interface to a running hub. Examples:

```bash
rb hub send state
rb hub send select demo_top.u_dma
rb hub send open-source design/dma.sv:84
rb hub send graph-focus module:dma_engine
rb hub send cov-focus file:design/dma.sv --line 84
rb hub send wave-add tb.dut.req tb.dut.ready
rb hub send wave-zoom 1000 2000
rb hub send capture --out schematic.png --format png
```

The command groups cover state broadcasts, waveform control and item management, schematic pan/overlay/capture, source opening, diagnostics, graph or coverage focus, and coordinate resolution. See the [CLI reference](../reference/cli.md#hub-send) for all verbs and arguments.

The hub caches the latest selection, graph focus, and coverage focus. You can send a focus before its app opens; it is replayed when the peer registers. Surfer-side rejection, an unknown id, or an unavailable target peer returns a real hub error and a non-zero exit.

## Design knowledge graph pane

Build the graph, start the browser layer, and open `/gph`:

```bash
rb graph build
rb graph results
rb hub start --serve-viewer
```

`GET /graph.json` reads `graph.json` and `results-overlay.json`, joins results and coverage in memory, and adds presentation categories. It returns 404 with a command hint if no graph exists. Reload the page after rebuilding or refreshing results.

Clicking graph nodes can:

- send `selection_changed` for an instance, or the shallowest instance of a module;
- send `open_source` for nodes with file locations;
- translate a selected node into coverage focus.

The on-disk graph is never modified by the browser join. See [Design Knowledge Graph](graph.md) for graph semantics.

## Coverage pane

Open `/cov` after a coverage-producing run. `GET /cov.json` uses the same coverage-model builder as `rb cov summary`, so CLI and browser totals agree. `GET /cov/source?path=...` serves only files named by that coverage model and only from under the project root.

The pane supports metric filtering, coldest-file ordering, a per-test lens, annotated source, and per-point attribution. Clicking source can send `source_focused` and `open_source`; clicking a module can send `graph_focus`. The hub resolves source locations into schematic selections where possible.

Coverage discovery is cached briefly. Reload after a run finishes if the landing page has not yet updated. See [Coverage](coverage.md) for collection and metric definitions.

## AXI-perf overlay and notebook spawning

Start with a canonical per-test `axi-perf.json` to add AXI performance data to generated schematics:

```bash
rb hub start --serve-viewer \
  --axi-perf-from <suite>/artefacts/axi/<test>/axi-perf.json
```

The file must exist at startup. Keeping the canonical layout lets the schematic identify the source test and launch its marimo notebook. The hub starts notebooks through `/api/axi-profile/notebook` and injects the local event-broker URL so schematic selections and the notebook remain synchronized.

See [AXI Interconnect Profiling](axi-profile.md#hub-integration) for how to produce the JSON and transaction Parquet files.

## Protocol and adapters

The protocol is UTF-8, line-delimited JSON over TCP or WebSocket. Its JSON Schema is `src/rtl_buddy/hub/schema/hub-protocol-v1.json`.

After connecting, a peer sends `hello`, receives `welcome`, and tracks `peer_joined` and `bye` updates. State events are broadcast to every peer except their origin. Requests are routed to the origin that owns the target coordinate system; an absent target returns `not_connected`.

The hub augments `source_focused` with resolved `selection_changed` events and relays producer-scoped `diagnostics_set` updates. `GET /healthz` is the liveness endpoint.

For a new adapter, validate envelopes against the schema and use `src/rtl_buddy/tools/wave_hub_bridge.py` as the narrow reference: connect, translate to the peer API, route, and reconnect.

## Auto-start on macOS

Install or remove the bundled LaunchAgent:

```bash
rb hub install-launchagent
rb hub uninstall-launchagent
```

The agent runs the hub from the project directory, restarts it when needed, and logs to `.rtl-buddy/hub.log`. These commands fail with `LaunchAgentUnsupportedError` on other platforms.

## Troubleshooting

- **Already running:** run `rb hub status`. Stop the live process, or remove `.rtl-buddy/hub.json` only if the recorded PID is stale.
- **Port in use:** choose a free fixed port in `hub.toml`, override it on the command line, or use `0` for OS assignment.
- **Peer cannot discover the hub:** set `RTL_BUDDY_HUB` to the `tcp` address in `hub.json`.
- **Wave bridge disconnected:** verify the supported Surfer fork is running with WCP enabled. The hub can stay running while the bridge reconnects.
- **Empty hub log:** foreground mode logs to the terminal. `--daemon` and the LaunchAgent redirect to the configured log file.
- **Viewer placeholder:** install `rtl-buddy-sch` or pass `--viewer-bundle PATH` for a development SPA build.
