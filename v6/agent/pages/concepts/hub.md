---
description: Start and operate the rtl_buddy hub, connect browser and editor peers, switch designs, send commands, and diagnose connection or view failures.
---

# Hub (`rb hub`)

The hub connects the schematic, waveform viewer, source editor, graph pane, coverage pane, and synth+power pane. It translates between their view, wave and source coordinates and routes live events among connected peers.

## Quick start

Start the hub from the project root:

```bash
uv run rb hub start --serve-viewer
```

Open the printed `http://127.0.0.1:<http_port>/` URL. The landing page links the apps:

| Route | App |
| --- | --- |
| `/sch` | Interactive schematic |
| `/gph` | Design knowledge graph |
| `/cov` | Coverage browser |
| `/phy` | Synthesis area and power browser |

From a second shell, inspect or stop the process:

```bash
uv run rb hub status
uv run rb hub log --follow
uv run rb hub stop
```

`rb hub start` stays in the foreground by default. `--daemon` detaches and logs to `.rtl-buddy/hub.log`; an early failure returns non-zero with the log tail.

The schematic needs `rtl-buddy-sch`, and live wave integration needs the rtl-buddy Surfer fork. See [Installation](../install.md#external-tools-by-feature) and [Waveform Viewer](wave.md).

## Start with a model or testbench

Generate and serve a schematic from `models.yaml` at startup:

```bash
rb hub start --serve-viewer --model ip_demo_tiny_npu
rb hub start --serve-viewer --model ip_demo_tiny_npu \
  --models-file design/npu/models.yaml
```

`--model` requires `--serve-viewer`. Without `--models-file`, the hub searches the project and requires exactly one matching model; zero or several matches fail and list the candidates. Pass `--models-file` when names overlap.

The browser can switch designs without a restart:

- `GET /models` lists models and their view status.
- `GET /view.json?model=NAME` builds or reuses the model's view and activates it.
- `GET /tests` lists runnable testbench views.
- `GET /view.json?test=NAME` builds and activates a view rooted at the test's testbench.

Views are cached; restart the hub after changing RTL. A failed build never serves the previous build's hierarchy.

## Diagnose view errors

A failed `GET /view.json` returns JSON with `error.kind`. Branch on the kind, not the prose:

| Kind | Meaning | Recovery |
| --- | --- | --- |
| `view_generation_failed` | Filelist, parse or elaboration failed. | Read `log_tail` or `log_path`, fix the model, then request it again. |
| `unknown_model` | No unique matching model exists. | Correct the name or pass `--models-file`. |
| `no_active_model` | No model or prebuilt view is selected. | Request `?model=NAME` or start with `--model`. |
| `no_project_root` | The hub cannot find project configuration. | Start inside the project. |

## Discovery and configuration

The running hub writes `.rtl-buddy/hub.json` with its PID, TCP address and HTTP port. Peers find it by walking up from their current directory. A peer outside the project tree sets `RTL_BUDDY_HUB=<host>:<port>` to the `tcp` value from `hub.json`; it is an address, not a file path.

Optional `.rtl-buddy/hub.toml`:

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

Port `0` lets the OS choose. Relative paths resolve from the project root. Signal aliases apply before `tb_prefix` is removed. Only `[hub]` and `[mapping]` are accepted: an unknown top-level section is an error, and unknown keys inside them are ignored. Check edits with `rb hub config validate`.

## Connect peers

Each adapter connects and reconnects itself; the hub only accepts connections.

| Peer | Origin | Transport |
| --- | --- | --- |
| Schematic SPA | `view` | WebSocket `/ws` |
| Graph pane | `graph` | WebSocket `/ws` |
| Coverage pane | `cov` | WebSocket `/ws` |
| Synth+power pane | `phys` | WebSocket `/ws` |
| `rb wave` bridge | `wave` | Line-delimited JSON over TCP |
| Editor adapter | `src` | Line-delimited JSON over TCP |
| `rb hub send` | `cli` | One-shot TCP client |

The hub allows one client per origin. A second browser tab takes over and disconnects the first, which stays disconnected until the user takes the connection back. `rb hub status` lists origins by protocol name (`view`, `graph`, `phys`), while the browser labels the same apps `sch`, `gph` and `phy`.

## Drive the hub from the CLI

`rb hub send` is the scripting interface to a running hub:

```bash
rb hub send state
rb hub send select demo_top.u_dma
rb hub send open-source design/dma.sv:84
rb hub send graph-focus module:dma_engine
rb hub send cov-focus file:design/dma.sv --line 84
rb hub send phys-focus module:dma_engine --metric area
rb hub send wave-add tb.dut.req tb.dut.ready
rb hub send capture --out schematic.png --format png
```

All verbs and arguments are in the [CLI reference](../reference/cli.md#hub-send).

The hub keeps the latest selection and the latest graph, coverage and physical focus. A focus sent before its app opens is delivered when the app connects. A Surfer rejection, an unknown id, or an absent target peer returns a hub error and a non-zero exit.

## Design knowledge graph pane

Build the graph, start the hub, and open `/gph`:

```bash
rb graph build
rb graph results
rb hub start --serve-viewer
```

`GET /graph.json` combines the graph, results overlay and coverage in memory without modifying the files on disk. It returns 404 with a command hint when no graph exists. Reload the page after rebuilding the graph or refreshing results. Graph semantics are in [Design Knowledge Graph](graph.md).

Clicking a node selects the instance (or the shallowest instance of a module) in the schematic, opens its source when it has a file location, and sets the coverage focus. A model with `graph: false` has no design coordinate, so its send buttons stay dark and say so.

Ticking `heat` colors module nodes by a synthesis or power metric from the [`/phy` pane](#synthpower-pane); `/gph?dir=<phys dir>` opens the overlay on one run. With no `phys-manifest.json` under the project, the control is muted and names `rb synth` and `rb power`; reload after producing one. Coverage and heat share the node fill, so enabling one releases the other.

## Coverage pane

Open `/cov` after a coverage-producing run. `GET /cov.json` uses the same builder as `rb cov summary`, so CLI and browser totals agree. `GET /cov/source?path=...` serves only files named by the coverage model, from under the project root.

The pane offers metric filtering, coldest-file ordering, a per-test view, annotated source, and per-point attribution. Its numbers are per elaboration; the run's source-point percentages are in the header tooltip. Clicking source opens the editor peer, and clicking a module focuses the graph pane.

Reload after a run finishes if the landing page has not updated. Collection and metric definitions are in [Coverage](coverage.md).

## Synth+power pane

Open `/phy` after `rb synth` or `rb power`. `GET /phy.json` uses the same builder as `rb phys summary`, so numbers agree with the CLI, but it never truncates: the pane sorts and filters the whole model in the browser. It returns 404 with a command hint when the project has no `phys-manifest.json`. Model and CLI verbs are in [Physical Metrics](phys.md).

The metrics are `cells`, `area`, `leakage`, `dynamic` and `total`. `dynamic` is internal plus switching. The totals header shows the flow's own total beside the sum of the rows and flags a disagreement instead of reconciling it.

A model with only one half still works. The pane names the command that fills the other half. If the power half came from a routed database (`netlist-source: pnr`), it cannot merge with a synthesis, so the banner tells you to synthesise first and rerun `rb power` on the new netlist.

Clicking a module focuses the graph pane, and clicking an instance selects it in the schematic. An inbound selection highlights the matching instance row.

### Select a run

The run dropdown in the pane header lists the 50 newest runs. `/phy.json` follows the newest run; `/phy.json?dir=<project-relative phys_dir>` selects another.

- A `dir` outside the project root returns 403, and a directory with no `phys-manifest.json` returns 404 and names `rb phys runs`.
- A refused selection keeps the current run on screen and reports the refusal in the status line.

## AXI-perf overlay and notebook spawning

Start with a per-test `axi-perf.json` to add AXI performance data to generated schematics:

```bash
rb hub start --serve-viewer \
  --axi-perf-from <suite>/artefacts/axi/<test>/axi-perf.json
```

The file must exist at startup and keep its canonical location under the test's artefacts, so the schematic can identify the test and launch its marimo notebook. Schematic selections and the notebook stay synchronized. To produce the JSON and transaction Parquet files, see [AXI Interconnect Profiling](axi-profile.md#hub-integration).

## Protocol and adapters

The protocol is UTF-8 line-delimited JSON over TCP or WebSocket. A peer sends `hello`, receives `welcome`, and tracks `peer_joined` and `bye`. State events go to every peer except their origin. Requests go to the origin that owns the target coordinate system; an absent target returns `not_connected`. `GET /healthz` is the liveness endpoint.

The JSON Schema is `src/rtl_buddy/hub/schema/hub-protocol-v1.json`, a vendored copy of [`rtl-buddy-sch/schemas/hub-protocol-v1.json`](https://github.com/rtl-buddy/rtl-buddy-sch/blob/main/schemas/hub-protocol-v1.json). Do not edit the copy; re-copy it from `rtl-buddy-sch`. A new adapter should validate envelopes against the schema and follow `src/rtl_buddy/tools/wave_hub_bridge.py`.

## Auto-start on macOS

```bash
rb hub install-launchagent
rb hub uninstall-launchagent
```

The LaunchAgent runs the hub from the project directory, restarts it, and logs to `.rtl-buddy/hub.log`. Other platforms fail with `LaunchAgentUnsupportedError`.

## Troubleshooting

- **Already running:** run `rb hub status`. Stop the live process, or remove `.rtl-buddy/hub.json` only if the recorded PID is stale.
- **Port in use:** choose a free fixed port in `hub.toml`, override it on the command line, or use `0` for OS assignment.
- **Peer cannot discover the hub:** set `RTL_BUDDY_HUB` to the `tcp` address in `hub.json`.
- **Wave bridge disconnected:** check that the supported Surfer fork is running with WCP enabled. The hub keeps running while the bridge reconnects.
- **Empty hub log:** foreground mode logs to the terminal; `--daemon` and the LaunchAgent write to the log file.
- **Viewer placeholder:** install `rtl-buddy-sch`, or pass `--viewer-bundle PATH` for a development SPA build.
- **`/graph.json` or `/phy.json` returns 404:** run the command the hint names (`rb graph build`, `rb synth`, `rb power`).
