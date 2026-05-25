---
description: rtl-buddy-hub is the broker that mediates between the rtl-buddy-view SPA, surfer (via rb wave), and editor adapters. Invocation, config, troubleshooting.
---

# Hub (`rb hub`)

> **Integration type:** Integrated tool. Ships in-tree at `src/rtl_buddy/hub/`; invoked via `rb hub start|stop|status|log|config validate`.
>
> **External binary required:** None for the hub itself. The wave adapter still needs the [`rtl-buddy/surfer`](https://github.com/rtl-buddy/surfer) fork for live WCP integration; see [Waveform Viewer](wave.md).
>
> **Default install carries it:** No external dependency; the hub is pure Python.

The **rtl-buddy-hub** is the broker between the [rtl-buddy-view](https://github.com/rtl-buddy/rtl-buddy-view) schematic viewer, the surfer waveform viewer (via the `rb wave` bridge), and editor adapters (nvim today, VS Code later). It owns the live coordinate-system translation (view ↔ wave ↔ source) and routes selection / cursor / scope events between every connected peer.

The hub is **server-only**: every external speaker connects *into* the hub. The hub itself never initiates an outbound connection. This keeps reconnection logic to a single "tolerate any peer reattaching at any time" rule and makes the dispatch surface transport-agnostic — TCP and WebSocket clients hit the same envelope router.

```
        ┌──────────────────────────────────┐
        │   rtl-buddy-view (browser SPA)   │
        └─────────────┬────────────────────┘
                      │ WebSocket /ws
                      ▼
        ┌──────────────────────────────────┐         ┌──────────────────────┐
        │       rtl-buddy-hub              │◀──TCP──▶│  rb wave bridge     │
        │       .rtl-buddy/hub.json        │         │  (surfer WCP)        │
        │       .rtl-buddy/hub.toml        │         └──────────────────────┘
        │                                  │         ┌──────────────────────┐
        │                                  │◀──TCP──▶│  nvim Lua plugin     │
        │                                  │         │  rtl_buddy_wave.lua  │
        └──────────────────────────────────┘         └──────────────────────┘
```

## Quick start

```bash
cd <project_root>
uv run rb hub start                   # foreground TCP server only
uv run rb hub start --serve-viewer    # also expose the viewer HTTP+WS endpoint
uv run rb hub status                  # in another shell: who's connected
uv run rb hub stop                    # graceful shutdown via SIGTERM
```

`rb hub start` runs in the foreground by default; backgrounding is the caller's job (`nohup rb hub start &`, a process manager, or — on macOS, planned — a LaunchAgent). The server binds the OS-assigned port (TCP, and HTTP if `--serve-viewer` is set) unless `hub.toml` pins them; the resolved port is written to `.rtl-buddy/hub.json` so peers can discover it.

## CLI surface

| Command | Purpose |
|---|---|
| `rb hub start [--foreground/--daemon] [--serve-viewer] [--viewer-bundle PATH] [--listen-port N] [--http-port N]` | Bind the TCP server (and optionally the viewer HTTP+WS layer), write `.rtl-buddy/hub.json`, run the asyncio loop. `--listen-port` / `--http-port` override `[hub].listen_port` / `[hub].http_port` from `hub.toml` (default 0 = OS-assigned). When a pinned port is already in use, the command prints a one-line error and exits 1 without a traceback. Exits cleanly on `SIGINT` / `SIGTERM` / `rb hub stop` and removes its discovery file. |
| `rb hub stop` | Send `SIGTERM` to the PID in `.rtl-buddy/hub.json`. |
| `rb hub status` | Print the current discovery record + liveness. Reports stale records (PID gone) so users know to clear them. |
| `rb hub log [--lines N] [--follow]` | Tail `.rtl-buddy/hub.log`. |
| `rb hub config validate [--path PATH]` | Schema-check `hub.toml` and exit non-zero on the first error. |

`--daemon` is reserved; today it warns and runs in the foreground. Treat the explicit `--foreground` as load-bearing; future versions may detach when `--daemon` is given.

`--serve-viewer` enables the HTTP + WebSocket layer (`/`, `/ws`) used by the browser SPA. When you omit `--viewer-bundle`, the hub auto-discovers the SPA shipped by [`rtl-buddy-view`](https://github.com/rtl-buddy/rtl-buddy-view) via `importlib.resources` — install it alongside rtl-buddy and `rb hub start --serve-viewer` is all you need. If rtl-buddy-view isn't installed (or you're on a checkout without a staged bundle), the hub falls back to a small placeholder page that proves the transport works. Pass `--viewer-bundle PATH` to override the auto-discovered bundle — useful when iterating on the SPA from a working tree (`viewer/dist/`) and you don't want the in-wheel copy from the installed package.

When the hub knows where to find a `view.json` (via `[mapping].view_json` in `hub.toml`, default `.rtl-buddy/view.json`), the viewer HTTP layer also serves it at `GET /view.json`. Open the SPA with `?view=/view.json` to auto-load the design — e.g. `http://127.0.0.1:<http_port>/?view=/view.json` — instead of drag-and-dropping the file. The index page also gets a `window.__RTL_BUDDY_VIEW_URL__ = "/view.json"` injection that a future SPA bootstrap can read directly without the query param. If the configured file is missing, `/view.json` returns 404 and the SPA falls back to the empty state.

### Picking a model at start time (`--model NAME`)

`--model NAME` tells the hub to generate `view.json` on the fly from a model entry in `models.yaml`, instead of relying on a pre-staged file:

```bash
rb hub start --serve-viewer --model ip_demo_tiny_npu
```

Resolution rules:

- The hub walks the project tree for every `models.yaml` it can find (skipping common build/VCS directories) and looks for an entry named `NAME`.
- Exactly one match → load it, generate `view.json` into `.rtl-buddy/cache/view-<model>.json`, serve it.
- Zero matches → error with the list of model names per discovered `models.yaml` so a typo is easy to spot.
- Two or more matches → error naming all the conflicting `models.yaml` paths. Pass `--models-file PATH` to disambiguate.

`--models-file PATH` skips the discovery walk entirely and loads the model from the named file. Use it when you have multiple `models.yaml` files in the tree with overlapping names.

`--model` requires `--serve-viewer` (the generated `view.json` is only useful as something the SPA HTTP layer can serve). Without `--serve-viewer` the hub errors at startup rather than silently discarding the generated file.

The view.json regenerates on every `rb hub start --model` invocation. Cache invalidation isn't modelled yet — restart the hub to pick up source-tree changes.

### Clock-domain overlay (`cdc:` back-pointer)

When the chosen model's `models.yaml` entry has a `cdc:` field, the hub also generates a clock-domain map and feeds it to the view-builder as `--cdc-annotations`:

```yaml
# models.yaml
rtl-buddy-filetype: model_config
models:
  - name: ip_demo_tiny_npu
    filelist: [...]
    cdc: cdc.yaml          # or cdc.yaml#analysis_name to pin one analysis
```

The hub:

1. Resolves the `cdc:` back-pointer to a `cdc.yaml` file.
2. Picks the analysis — either the one named by the optional `#fragment`, or the one whose `model:` field matches the model name. Ambiguity is a hard error (the message tells you to add a `#fragment`).
3. Invokes `rtl-buddy-cdc lint --emit-domain-map .rtl-buddy/cache/domain-<model>.json ...` with the analysis's SDC + waivers.
4. Passes the resulting domain map to `rtl-buddy-view --cdc-annotations`. The clock overlay toggle in the SPA then has data to render.

Models without a `cdc:` field skip this step entirely — view.json is generated without overlays and the toggle stays dark. `rtl-buddy-cdc` must be on `PATH` when the `cdc:` field is present; absence is a hub-start error (no silent dark toggle).

### Switching models at runtime

Once the hub is up, the SPA can change models without restarting:

- `GET /models` — list every model the hub can serve. JSON shape:
  ```json
  {
    "models": [
      {"name": "ip_demo_tiny_npu", "models_file": "/abs/path/to/models.yaml", "has_cdc": true},
      {"name": "ip_dtnpu_dma",     "models_file": "/abs/path/to/models.yaml", "has_cdc": true}
    ],
    "active": "ip_demo_tiny_npu"
  }
  ```
  `has_cdc` is end-to-end: `true` only when the model has a `cdc:` field AND the referenced cdc.yaml exists AND at least one analysis resolves cleanly for the model. The endpoint walks for `models.yaml` per request, so newly-edited files appear without a restart. When `--models-file PATH` was passed at start time, only that file is enumerated.
- `GET /view.json?model=NAME` — build (or reuse) the per-model view.json at `.rtl-buddy/cache/view-<NAME>.json`, serve it, and promote `NAME` to the active model. `--models-file` constraints apply: `?model=` only honours entries in the pinned file. Per-model `asyncio.Lock` serialises concurrent same-model requests so a cold-cache race doesn't run rtl-buddy-view twice for the same model.
- `GET /tests` — list every test the hub can serve (rtl-buddy-view #99 / 6b). Same per-request walk as `/models`; entries carry the resolved `(model, tb)` pair so the SPA's TB-mode picker can label options. Empty list signals "no tests advertised" — the SPA's DUT/TB toggle stays hidden. JSON shape:
  ```json
  {
    "tests": [
      {"name": "basic", "model": "ip_demo_tiny_npu", "tb": "tb_top", "tests_file": "/abs/path/to/tests.yaml"}
    ],
    "active": "basic"
  }
  ```
- `GET /view.json?test=NAME` — build (or reuse) the per-`(model, tb)` view.json at `.rtl-buddy/cache/view-<MODEL>-tb-<TB>.json`, serve it, and promote the test (and its underlying model) to active. Per-test `asyncio.Lock` mirrors the per-model lock. The renderer runs in TB-rooted mode: rtl-buddy-view is invoked with `--top <model>` + `--tb-top <tb.toplevel>` so the rendered tree is rooted at the testbench top with the DUT recorded for the SPA's dashed-boundary overlay.
- `view_changed` event — broadcast on every active-view change. Envelope:
  ```json
  {"v":1, "id":"…", "origin":"cli", "kind":"event", "type":"view_changed",
   "payload":{"model":"ip_dtnpu_dma", "models_file":"/abs/path/to/models.yaml",
              "view_url":"/view.json?model=ip_dtnpu_dma",
              "view_mode":"dut"}}
  ```
  In TB-view mode (`?test=` switch) the payload carries `view_mode: "tb"` plus `test` + `tb` + `tests_file` fields (the `view_url` points at `/view.json?test=<NAME>`). v1.0 SPAs that don't know about `view_mode` ignore it and fall through to the legacy `model`-driven `switchModel` path — that's why the DUT-side envelope still carries the full set of legacy fields. Sent to every connected client (SPA tabs, nvim, `rb wave` bridge) so they can refresh view-scoped state.

The active model is also recorded in `.rtl-buddy/hub.json` under `active_model` (optional field) and surfaced in `rb hub status` output.

## Discovery (`.rtl-buddy/hub.json`)

When the hub binds, it writes a small JSON record under the project root's `.rtl-buddy/` directory:

```json
{
  "pid": 41231,
  "listen_port": 53201,
  "http_port": 53202,
  "started_at": "2026-05-19T12:34:56Z",
  "project_root": "/path/to/project",
  "active_model": "ip_demo_tiny_npu"
}
```

`active_model` is optional — present when the hub started with `--model NAME` or after a `GET /view.json?model=` switch.

Peers (the viewer SPA, the `rb wave` bridge, the nvim plugin) read this file to find the hub. The hub deletes the record on clean shutdown; a stale record after a crash is detected by `rb hub status` (PID not live) and the next `rb hub start` overwrites it.

Override discovery resolution with the `RTL_BUDDY_HUB` environment variable when running outside a project tree — it should point at a `hub.json` directly.

## Configuration (`.rtl-buddy/hub.toml`)

Optional; sensible defaults apply when the file is absent. Two top-level sections:

```toml
[hub]
listen_port = 0          # 0 = OS-assigned (default). Pin to a specific port to survive across restarts.
http_port   = 0          # Same, for the viewer HTTP+WS layer (only used with --serve-viewer).
log_path    = ".rtl-buddy/hub.log"   # Relative paths resolve from the project root.

[mapping]
tb_prefix   = "tb.dut."  # Fallback for DUT-rooted views. When the loaded view.json carries tb_top (rtl-buddy-view v1.1, #99 / 6b), the resolver short-circuits to identity wave↔view mapping and tb_prefix is bypassed — the rendered TB tree already speaks the wave-side coordinate system.
view_json   = ".rtl-buddy/view.json"  # Snapshot the resolver consumes. Defaults shown.

# Optional pre-strip aliases — applied before tb_prefix is stripped.
[[mapping.signal_aliases]]
wave = "tb.legacy_dut.clk"
view = "tb.dut.clk"
```

Unknown top-level sections fail validation (typo guard). Unknown keys *inside* known sections are tolerated for forward-compat. `rb hub config validate` runs the same loader and reports errors with file:line context.

## Peers (who connects to the hub)

| Peer | Transport | How it connects |
|---|---|---|
| **rtl-buddy-view SPA** (browser) | WebSocket `/ws` on the hub's `http_port` | Loaded from the bundle when `rb hub start --serve-viewer` is in use. The bundle is injected with `window.__RTL_BUDDY_HUB__` at serve time. |
| **`rb wave` bridge** (`tools/wave_hub_bridge.py`) | Line-delimited JSON over TCP on `listen_port` | Started by `rb wave`; bridges surfer's WCP TCP socket to the hub. Reconnect with backoff. |
| **nvim plugin** (`src/rtl_buddy/nvim/rtl_buddy_wave.lua`) | Line-delimited JSON over TCP on `listen_port` | Connects when the user opens a file rtl-buddy knows how to resolve. |

Each peer has a closed `Origin` enum value (`view`, `wave`, `nvim`, …); the hub allows at most one client per origin in v1.

## Protocol

Wire envelope is line-delimited JSON, one record per line, UTF-8. The full spec lives in [rtl-buddy/rtl-buddy-view#19](https://github.com/rtl-buddy/rtl-buddy-view/issues/19); the JSON Schema enforcing it ships at `src/rtl_buddy/hub/schema/hub-protocol-v1.json`. Encoded and decoded by `rtl_buddy.hub.protocol`, which validates on both sides — unknown fields are caller bugs, not forward-compat points.

State events (selection_changed, signal_selected, cursor_moved, …) are broadcast to every connected peer **except** the origin. Requests (`resolve_*`, `goto_declaration`, …) are routed to the peer whose origin owns the target coordinate system; if no peer is registered for that origin, the hub replies with `error{code: "not_connected"}`. The `view ↔ wave ↔ src` resolver lives in `rtl_buddy.hub.resolver` and consumes the `view.json` snapshot pointed at by `mapping.view_json`.

Lifecycle events (`hello` / `welcome` / `peer_joined` / `bye`) keep each peer's view of the registry live without re-fetching: `welcome` carries the snapshot at handshake time, and `peer_joined` / `bye` are deltas the hub broadcasts when later peers connect or disconnect. The joining or leaving peer's origin is in the envelope's `origin` field (payload is empty). Consumers should react to all three to maintain a current peer list — relying on `welcome` alone leaves the list frozen at handshake time.

The hub also **augments `source_focused`**: when a `src` peer (e.g. nvim's `:RtlBuddyShow`) broadcasts `{file, line, col}`, the resolver looks up the instance(s) whose `source` range in `view.json` contains the point and the hub emits a derived `selection_changed { instance_path: [...] }` with `origin: "cli"`. The schematic SPA already handles `selection_changed` — pan/highlight the matching instance — so this bridge makes editor cursor movement light up the schematic without a SPA-side protocol change. Multiple matches (nested instances) come back smallest-range first; consumers picking element `[0]` get the most-specific instance. Line-only matching is used for multi-line ranges (cursor at column 1 still finds an instantiation whose keyword sits further right); single-line ranges still use columns so two instantiations on the same line resolve distinctly.

## Troubleshooting

**`rb hub start` exits with "already running"** — `.rtl-buddy/hub.json` exists and its PID is live. If the prior daemon really is gone, the file is stale (clean shutdown didn't run); delete it and retry. `rb hub status` distinguishes the two cases.

**Port already in use** — pin `listen_port` (and `http_port` if using `--serve-viewer`) to a free port in `hub.toml`, or leave them at `0` to let the OS pick. The chosen port lands in `hub.json` either way.

**Peer can't find the hub from outside the project tree** — set `RTL_BUDDY_HUB=/path/to/.rtl-buddy/hub.json` in the peer's environment. The default discovery walks up from `cwd` looking for `.rtl-buddy/hub.json`, which doesn't work for processes launched from elsewhere.

**`rb wave` bridge reports surfer disconnected** — the bridge owns the WCP TCP connection, not the hub. Check the surfer fork is on PATH and built with WCP support; see [Waveform Viewer](wave.md). The hub stays up regardless; reconnect is automatic.

**`rb hub config validate` reports "unknown section"** — typo. The schema accepts exactly `[hub]` and `[mapping]`; everything else (surfer flags, nvim keymaps, …) belongs in the adapters' own config.

**Hub log empty or absent** — `rb hub log` tails `.rtl-buddy/hub.log` by default. The `[hub].log_path` setting controls the location; logs route through `log_event()` like the rest of `rtl_buddy`, so `--machine` mode produces JSON Lines.

## Writing a new adapter

Bring up a TCP client against the hub's `listen_port`, send the `hello` envelope claiming an origin, accept the `welcome` reply, then send / receive state events and requests per [rtl-buddy-view#19](https://github.com/rtl-buddy/rtl-buddy-view/issues/19). The JSON Schema at `src/rtl_buddy/hub/schema/hub-protocol-v1.json` is the contract — validate against it on both sides and unknown `type` strings should be silently dropped (forward-compat rule from §11 of the spec).

The existing peers — `tools/wave_hub_bridge.py` and `src/rtl_buddy/nvim/rtl_buddy_wave.lua` — are the reference adapters. Both stay narrow on purpose: parse the envelope, translate to the peer's native API, route, repeat.

## Reference

- Wire protocol spec: [rtl-buddy-view#19](https://github.com/rtl-buddy/rtl-buddy-view/issues/19)
- JSON Schema: `src/rtl_buddy/hub/schema/hub-protocol-v1.json`
- Implementation: `src/rtl_buddy/hub/`
- Wave bridge: `src/rtl_buddy/tools/wave_hub_bridge.py`, `src/rtl_buddy/tools/wave_launcher.py`
- nvim plugin: `src/rtl_buddy/nvim/rtl_buddy_wave.lua`
