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
| `/sch` | Interactive schematic. |
| `/gph` | Design knowledge graph. |
| `/cov` | Coverage browser. |
| `/phy` | Synthesis area and power browser. |

From a second shell, inspect or stop the process:

```bash
uv run rb hub status
uv run rb hub log --follow
uv run rb hub stop
```

`rb hub start` stays in the foreground by default. `--daemon` detaches and logs to `.rtl-buddy/hub.log`; startup waits until the process publishes discovery, and an early failure returns non-zero with the log tail.

The hub itself needs no external binary. The schematic needs `rtl-buddy-sch`, and live wave integration needs the rtl-buddy Surfer fork. See [Installation](../install.md#external-tools-by-feature) and [Waveform Viewer](wave.md).

## Start with a model or testbench

Generate and serve a schematic from `models.yaml` at startup:

```bash
rb hub start --serve-viewer --model ip_demo_tiny_npu
rb hub start --serve-viewer --model ip_demo_tiny_npu \
  --models-file design/npu/models.yaml
```

`--model` requires `--serve-viewer`. Without `--models-file`, the hub searches the project and requires exactly one matching model. Zero or several matches fail and list the discovered files and model names. Pass `--models-file` to constrain discovery when names overlap.

The browser can switch designs without a restart:

- `GET /models` lists models and their view status.
- `GET /view.json?model=NAME` builds or reuses `.rtl-buddy/cache/view-<NAME>.json`, activates it, and broadcasts `view_changed`.
- `GET /tests` lists runnable testbench views.
- `GET /view.json?test=NAME` builds and activates a testbench-rooted view from the test's model and testbench.

Model discovery is refreshed per request, and generation is serialized per model or test. To regenerate after source changes, restart the hub.

A rebuild first clears the cached view and the model's domain map. A build that fails, or produces a view this rtl_buddy rejects, therefore reports the failure and never serves the previous build's hierarchy.

## Diagnose view errors

A failed `GET /view.json` returns JSON with `error.kind`. Branch on the kind, not the prose:

| Kind | Meaning | Recovery |
| --- | --- | --- |
| `view_generation_failed` | Filelist, parse or elaboration failed. | Read `log_tail` or `log_path`, fix the model, then restart or request it again. |
| `unknown_model` | No unique matching model exists. | Correct the name or pass `--models-file`. |
| `no_active_model` | No model or prebuilt view is selected. | Request `?model=NAME` or start with `--model`. |
| `no_project_root` | The hub cannot find project configuration. | Start inside the project or give the correct root context. |

`view_generation_failed` includes the last renderer log lines. Common causes are unsupported parser syntax, missing submodules, and filelist entries the renderer cannot consume.

## Discovery and configuration

After binding, the hub writes `.rtl-buddy/hub.json` with the PID, TCP address, project root, server version, and the HTTP port and active model when set. Peers find it by walking up from their current directory. A peer outside the project tree sets `RTL_BUDDY_HUB=<host>:<port>` to the `tcp` value from `hub.json`; it is an address, not a file path.

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

Port `0` lets the OS choose. Relative paths resolve from the project root. Signal aliases apply before `tb_prefix` is removed. Only `[hub]` and `[mapping]` are valid top-level sections; unknown keys inside them are tolerated. Validate edits with:

```bash
rb hub config validate
```

## Connect peers

The hub only accepts inbound connections; each adapter connects and reconnects itself.

| Peer | Origin | Transport |
| --- | --- | --- |
| Schematic SPA | `view` | WebSocket `/ws`. |
| Graph pane | `graph` | WebSocket `/ws`. |
| Coverage pane | `cov` | WebSocket `/ws`. |
| Synth+power pane | `phys` | WebSocket `/ws`. |
| `rb wave` bridge | `wave` | Line-delimited JSON over TCP. |
| Editor adapter | `src` | Line-delimited JSON over TCP. |
| `rb hub send` | `cli` | One-shot TCP client. |

The hub allows one client per origin. A second browser tab takes over and disconnects the first, which stops reconnecting until the user takes the connection back. The landing page registers no origin, so it never evicts an app.

`rb hub status` lists live origins by protocol name (`view`, `graph`, `phys`), while the browser labels the same apps `sch`, `gph` and `phy`.

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
rb hub send wave-zoom 1000 2000
rb hub send capture --out schematic.png --format png
```

The verbs cover state broadcasts, waveform control, schematic pan, overlay and capture, source opening, diagnostics, graph, coverage and physical focus, and coordinate resolution. All verbs and arguments are in the [CLI reference](../reference/cli.md#hub-send).

The hub caches the latest selection and the graph, coverage and physical focus, one slot each with the latest writer winning. A focus sent before its app opens is delivered when the peer registers, and a late-joining pane opens on the most recent target. A Surfer rejection, an unknown id, or an unavailable target peer returns a hub error and a non-zero exit.

## Design knowledge graph pane

Build the graph, start the browser layer, and open `/gph`:

```bash
rb graph build
rb graph results
rb hub start --serve-viewer
```

`GET /graph.json` joins `graph.json`, `results-overlay.json` and coverage in memory and adds presentation categories; the on-disk graph is never modified. It returns 404 with a command hint when no graph exists. Reload the page after rebuilding the graph or refreshing results. Graph semantics are in [Design Knowledge Graph](graph.md).

Clicking a node can:

- send `selection_changed` for an instance, or the shallowest instance of a module;
- send `open_source` for nodes with file locations;
- translate the selection into coverage focus.

A model node maps to the module the model roots at, even when `models.yaml` sets `top:`. A model with `graph: false` has no design coordinate, so its send buttons stay dark and say so.

Ticking `heat` fetches `GET /phy.json` on first use and colors module nodes with the shared heat ramp. Cells and area come from the synthesis half by module name, counted once per module definition. Power sums every leaf row inside every instantiation of the module. The inspector shows how many instantiations and rows each figure covers.

- The metric switcher and run dropdown are the `/phy` pane's. `/gph?dir=<phys dir>` opens the pane on one run. A refused run keeps the current model on screen and reports the refusal; a bad `?dir=` before anything loads falls back to the newest run.
- With no `phys-manifest.json` under the project, the control is muted and names `rb synth` and `rb power`. A model produced after the tab opened needs a reload.
- Coverage and heat share the node fill, so enabling one releases the other.
- An inbound `phys_focus` turns the overlay on, loads the model if needed, and highlights the node the target belongs to: a module by name, or an instance path by the module holding the leaf.

## Coverage pane

Open `/cov` after a coverage-producing run. `GET /cov.json` uses the builder behind `rb cov summary`, so CLI and browser totals agree. `GET /cov/source?path=...` serves only files named by the coverage model and only from under the project root.

The pane offers metric filtering, coldest-file ordering, a per-test lens, annotated source, and per-point attribution. Its numbers are per elaboration; the run's source-point percentages appear in the header tooltip. Clicking source sends `source_focused` and `open_source`, and clicking a module sends `graph_focus`. The hub resolves source locations to schematic selections where possible.

Coverage discovery is cached briefly, so reload after a run finishes if the landing page has not updated. Collection and metric definitions are in [Coverage](coverage.md).

## Synth+power pane

Open `/phy` after `rb synth` or `rb power`. `GET /phy.json` uses the builder behind `rb phys summary`, so numbers agree with the CLI. Unlike the CLI it truncates nothing, because the pane sorts and filters the whole model in the browser. It returns 404 with a command hint when the project has no `phys-manifest.json`. Model and CLI verbs are in [Physical Metrics](phys.md).

Physical discovery is cached briefly, like coverage.

## Select a synth+power run

`?dir=<project-relative phys_dir>` selects one run. Bare `/phy.json` serves the newest manifest.

- A path outside the project root is 403. A directory with no `phys-manifest.json` is 404 and names `rb phys runs`.
- A symlink is followed only for an `artefacts` component below the project root, the layout discovery already walks. Any other link that resolves outside the project, such as `vendor/` or `$HOME`, is 403. A `..` is refused.
- The response includes the `rb phys runs` listing (50 newest, with the total count), shown as a dropdown in the pane header. Entries read `run · top · backends · mode (activity) · experiment`, and hovering shows the artefact directory, configuration fingerprint and timestamp.
- Choosing an entry re-fetches with `?dir=`, except the newest, which selects the bare route so the pane follows whichever run finishes next. A refused selection keeps the current run and reports the refusal in the status line.
- An inbound `phys_focus` names a target and metric but no run, so it applies to the run on display. Switching runs is done only in the pane.

## Read the synth+power metrics

The metrics are `cells`, `area`, `leakage`, `dynamic` and `total`. `dynamic` is internal plus switching, summed in the browser. The totals header shows the flow's scraped total beside the sum of the rows and flags a disagreement instead of reconciling it, because the two come from different scrapes.

A model with only one half still works. The pane names the command that fills the other half, unless that command could not merge. A power half from a routed database (`netlist-source: pnr`) records no netlist hash, so the banner tells you to synthesise first and rerun `rb power` on the new netlist. `rb hub send phys-focus` drives whichever half is present.

Clicking a module sends `graph_focus`, and clicking an instance sends `selection_changed`. An inbound `selection_changed` highlights the matching instance row.

A reload re-reads the run on display, or follows discovery if none is selected. If it lands on the same model, metric, sort, filter, module lens and selected instance are kept. If it lands on a different model, such as a newer run or a republished one, the controls stay and the lens and selection are dropped. A `phys-focus` sent while the pane was loading applies to the model that arrives.

## AXI-perf overlay and notebook spawning

Start with a canonical per-test `axi-perf.json` to add AXI performance data to generated schematics:

```bash
rb hub start --serve-viewer \
  --axi-perf-from <suite>/artefacts/axi/<test>/axi-perf.json
```

The file must exist at startup. Keep the canonical layout so the schematic can identify the source test and launch its marimo notebook. The hub starts notebooks through `/api/axi-profile/notebook` and passes the local event-broker URL, so schematic selections and the notebook stay synchronized.

To produce the JSON and transaction Parquet files, see [AXI Interconnect Profiling](axi-profile.md#hub-integration).

## Protocol and adapters

The protocol is UTF-8 line-delimited JSON over TCP or WebSocket. Its JSON Schema is `src/rtl_buddy/hub/schema/hub-protocol-v1.json`, a vendored copy of [`rtl-buddy-sch/schemas/hub-protocol-v1.json`](https://github.com/rtl-buddy/rtl-buddy-sch/blob/main/schemas/hub-protocol-v1.json). Re-copy it byte-for-byte instead of editing it.

The schema's `origin` vocabulary is also hand-copied into the `Origin` enum in `hub/protocol.py`, and `tests/test_hub_protocol.py::test_origin_enum_matches_vendored_schema` checks the two agree. Adding an origin is a coordinated change across three repositories, with the schema merged first and this repository last. The checklist is [`docs/hub-protocol.md` section 13](https://github.com/rtl-buddy/rtl-buddy-sch/blob/main/docs/hub-protocol.md#13-adding-or-renaming-an-origin--lockstep-checklist) in that repository.

A peer sends `hello`, receives `welcome`, and tracks `peer_joined` and `bye`. State events go to every peer except their origin. Requests go to the origin that owns the target coordinate system, and an absent target returns `not_connected`. The hub adds resolved `selection_changed` events to `source_focused` and relays producer-scoped `diagnostics_set` updates. `GET /healthz` is the liveness endpoint.

For a new adapter, validate envelopes against the schema and follow `src/rtl_buddy/tools/wave_hub_bridge.py`: connect, translate to the peer API, route, and reconnect.

## Auto-start on macOS

```bash
rb hub install-launchagent
rb hub uninstall-launchagent
```

The bundled LaunchAgent runs the hub from the project directory, restarts it when needed, and logs to `.rtl-buddy/hub.log`. On other platforms these commands fail with `LaunchAgentUnsupportedError`.

## Troubleshooting

- **Already running:** run `rb hub status`. Stop the live process, or remove `.rtl-buddy/hub.json` only if the recorded PID is stale.
- **Port in use:** choose a free fixed port in `hub.toml`, override it on the command line, or use `0` for OS assignment.
- **Peer cannot discover the hub:** set `RTL_BUDDY_HUB` to the `tcp` address in `hub.json`.
- **Wave bridge disconnected:** check that the supported Surfer fork is running with WCP enabled. The hub keeps running while the bridge reconnects.
- **Empty hub log:** foreground mode logs to the terminal. `--daemon` and the LaunchAgent write to the configured log file.
- **Viewer placeholder:** install `rtl-buddy-sch`, or pass `--viewer-bundle PATH` for a development SPA build.
