---
description: rtl-buddy-hub is the broker that mediates between the rtl-buddy-view SPA, surfer (via rb wave), and editor adapters. Invocation, config, troubleshooting.
---

# Hub (`rb hub`)

> **Integration type:** Integrated tool. Ships in-tree at `src/rtl_buddy/hub/`; invoked via `rb hub start|stop|status|log|install-launchagent|uninstall-launchagent|config validate|send …`.
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
        │                                  │         │  (rtl-buddy-nvim)    │
        └──────────────────────────────────┘         └──────────────────────┘
```

## Quick start

```bash
cd <project_root>
uv run rb hub start                   # foreground TCP server only
uv run rb hub start --serve-viewer    # also expose the browser apps + WS endpoint
uv run rb hub status                  # in another shell: who's connected
uv run rb hub stop                    # graceful shutdown via SIGTERM
```

With `--serve-viewer`, open `http://127.0.0.1:<http_port>/` — the [landing page](#apps-the-landing-page-and-hub-chrome) lists every app this hub can serve (`rtl-buddy-schematic`, `rtl-buddy-graph`, `rtl-buddy-coverage`), says which one already has a tab attached, and shows the project state.

`rb hub start` runs in the foreground by default; backgrounding is the caller's job (`nohup rb hub start &`, a process manager, or — on macOS — the bundled LaunchAgent: see [`rb hub install-launchagent`](#auto-start-on-macos-launchagent)). The server binds the OS-assigned port (TCP, and HTTP if `--serve-viewer` is set) unless `hub.toml` pins them; the resolved TCP address (and HTTP port, with `--serve-viewer`) is written to `.rtl-buddy/hub.json` so peers can discover it.

## CLI surface

| Command | Purpose |
|---|---|
| `rb hub start [--foreground/--daemon] [--serve-viewer] [--viewer-bundle PATH] [--listen-port N] [--http-port N] [--model NAME] [--models-file PATH] [--axi-perf-from PATH]` | Bind the TCP server (and optionally the viewer HTTP+WS layer), write `.rtl-buddy/hub.json`, run the asyncio loop. `--listen-port` / `--http-port` override `[hub].listen_port` / `[hub].http_port` from `hub.toml` (default 0 = OS-assigned). `--axi-perf-from` bakes an AXI-perf overlay into served views (see [AXI-perf overlay & notebook spawning](#axi-perf-overlay-and-notebook-spawning)). When a pinned port is already in use, the command prints a one-line error and exits 1 without a traceback. Exits cleanly on `SIGINT` / `SIGTERM` / `rb hub stop` and removes its discovery file. |
| `rb hub stop` | Send `SIGTERM` to the PID in `.rtl-buddy/hub.json`. |
| `rb hub status` | Print the current discovery record + liveness, the landing (`hub_url`) and SPA (`viewer_url`) URLs, and which peers are connected. Reports stale records (PID gone) so users know to clear them. |
| `rb hub log [--lines N] [--follow]` | Tail `.rtl-buddy/hub.log`. |
| `rb hub install-launchagent` | (macOS) Install a LaunchAgent so the hub auto-starts at login. See [Auto-start on macOS](#auto-start-on-macos-launchagent). |
| `rb hub uninstall-launchagent` | (macOS) Remove the LaunchAgent. |
| `rb hub config validate [--path PATH]` | Schema-check `hub.toml` and exit non-zero on the first error. |
| `rb hub send <verb> …` | One-shot peer that connects as `origin=cli` to drive the running hub from scripts. See [Driving the hub from the CLI](#driving-the-hub-from-the-cli-rb-hub-send). |

`--daemon` is reserved; today it warns and runs in the foreground. Treat the explicit `--foreground` as load-bearing; future versions may detach when `--daemon` is given.

`--serve-viewer` enables the HTTP + WebSocket layer (`/`, `/view`, `/graph`, `/ws`) used by the browser apps. When you omit `--viewer-bundle`, the hub auto-discovers the SPA shipped by [`rtl-buddy-view`](https://github.com/rtl-buddy/rtl-buddy-view) via `importlib.resources` — install it alongside rtl-buddy and `rb hub start --serve-viewer` is all you need. If rtl-buddy-view isn't installed (or you're on a checkout without a staged bundle), the hub falls back to a small placeholder page that proves the transport works. Pass `--viewer-bundle PATH` to override the auto-discovered bundle — useful when iterating on the SPA from a working tree (`viewer/dist/`) and you don't want the in-wheel copy from the installed package.

When the hub knows where to find a `view.json` (via `[mapping].view_json` in `hub.toml`, default `.rtl-buddy/view.json`), the viewer HTTP layer also serves it at `GET /view.json`. Open the SPA with `?view=/view.json` to auto-load the design — e.g. `http://127.0.0.1:<http_port>/view?view=/view.json` — instead of drag-and-dropping the file. The index page also gets a `window.__RTL_BUDDY_VIEW_URL__ = "/view.json"` injection that a future SPA bootstrap can read directly without the query param. If the configured file is missing and no model has been selected, `/view.json` returns `409 no_active_model` (see [View errors](#view-errors)) and the SPA shows its "pick a model" placeholder.

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

### Switching models at runtime

Once the hub is up, the SPA can change models without restarting:

- `GET /models` — list every model the hub can serve. JSON shape:
  ```json
  {
    "models": [
      {"name": "ip_demo_tiny_npu", "models_file": "/abs/path/to/models.yaml",
       "has_cdc": false, "view_status": "ok"},
      {"name": "apb_intf", "models_file": "/abs/path/to/models.yaml",
       "has_cdc": false, "view_status": "failed",
       "error": "rb hub --model apb_intf: rtl-buddy-view exited with code 1; see …/hier.log for details."}
    ],
    "active": "ip_demo_tiny_npu"
  }
  ```
  The endpoint walks for `models.yaml` per request, so newly-edited files appear without a restart. When `--models-file PATH` was passed at start time, only that file is enumerated.

  `view_status` is the model's **health**, so the picker can badge a model that can never elaborate instead of letting the user find out via an empty canvas (rtl-buddy-view#130):

  | value | meaning |
  | --- | --- |
  | `ok` | a cached `.rtl-buddy/cache/view-<NAME>.json` exists, or this hub session generated one successfully |
  | `failed` | this hub session tried to generate a view for the model and the generation failed; `error` carries the one-line summary |
  | `never_built` | neither — nobody has asked for this model yet |

  A `failed` entry that still has a cache file from an earlier build also carries `"stale_cache": true` — the cached tree is servable but no longer reflects the sources. Health is remembered **in memory** for the hub session; a restart resets every model to what the cache on disk says.
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

### View errors

Every `GET /view.json` failure answers with `Content-Type: application/json` and one shape, so the SPA can render a "no view available" placeholder that says *why* instead of an empty canvas (rtl-buddy-view#130):

```json
{"error": {"kind": "…", "message": "…"}}
```

Branch on `kind`, never on the status code or the prose:

| `kind` | status | extra keys | when |
| --- | --- | --- | --- |
| `view_generation_failed` | 500 | `model`, `log_path`, `log_tail` | rtl-buddy-view (or the filelist that feeds it) refused the model |
| `unknown_model` | 404 | `model` | `?model=NAME` resolves to zero models, or to more than one `models.yaml` |
| `no_active_model` | 409 | `models_url` | bare `GET /view.json` with nothing selected and no pre-staged `view.json` |
| `no_project_root` | 400 | `model` | `?model=` on a hub started without a project root |

`log_tail` is the last 40 lines of `log_path` (`artefacts/hier/<model>/hier.log`) as a list of strings, or `[]` when the log is unreadable. It is the actionable half: the recurring causes name themselves there — an interface-port top that needs `frontend: slang`, a vendor submodule nobody ran `git submodule update --init` on, a `-v` library entry the parser does not accept.

```json
{"error": {"kind": "view_generation_failed",
           "model": "apb_intf",
           "message": "rb hub --model apb_intf: rtl-buddy-view exited with code 1; see /abs/artefacts/hier/apb_intf/hier.log for details.",
           "log_path": "/abs/artefacts/hier/apb_intf/hier.log",
           "log_tail": ["$ rtl-buddy-view --top apb_intf …",
                        "hierarchy: top module 'apb_intf' not found. Known modules: []"]}}
```

A failure is remembered for the hub session, so the bare `GET /view.json` replays the same body for the active model rather than answering `409` — the named-model and active-model request paths never disagree about what the hub can show.

## Discovery (`.rtl-buddy/hub.json`)

When the hub binds, it writes a small JSON record under the project root's `.rtl-buddy/` directory:

```json
{
  "v": 1,
  "pid": 41231,
  "tcp": "127.0.0.1:53201",
  "server_version": "0.5.0",
  "project_root": "/path/to/project",
  "started_at": "2026-05-19T12:34:56+00:00",
  "http_port": 53202,
  "active_model": "ip_demo_tiny_npu"
}
```

The TCP listener address is the single `tcp` `host:port` string (there is no `listen_port` field). `v` is the discovery-schema version and `server_version` is the hub build. `http_port` is present only when the hub was started with `--serve-viewer`; `active_model` is present when the hub started with `--model NAME` or after a `GET /view.json?model=` switch (both optional keys are omitted when unset).

Peers (the viewer SPA, the `rb wave` bridge, the nvim plugin) read this file to find the hub. The hub deletes the record on clean shutdown; a stale record after a crash is detected by `rb hub status` (PID not live) and the next `rb hub start` overwrites it.

Override discovery resolution with the `RTL_BUDDY_HUB` environment variable when running outside a project tree — set it to the hub's `host:port` (the `tcp` value from `hub.json`, e.g. `RTL_BUDDY_HUB=127.0.0.1:53201`), **not** a path to a file.

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

## Apps, the landing page, and hub chrome

The hub serves more than one browser app, so `GET /` is a **landing page** that names the tasks and routes to the app that does each one. The schematic SPA moved to `/view` in [rtl-buddy/rtl_buddy#398](https://github.com/rtl-buddy/rtl_buddy/issues/398) (it used to be `/`); `rb hub start --serve-viewer` prints both URLs, and `rb hub status` reports `hub_url` alongside `viewer_url`.

| Route | Serves |
|---|---|
| `/` | The landing page: task cards, live hub state, "already open" warnings. |
| `/view` (`/index.html` is an alias) | The rtl-buddy-view SPA (or the placeholder page when no bundle is installed). |
| `/graph` | The [design knowledge graph pane](#design-knowledge-graph-pane). |
| `/cov` | The [coverage pane](#coverage-pane). |
| `/hub/state.json` | The landing page's data: hub identity, active model, connected peers, per-app availability, graph freshness. |
| `/hub/theme.css` | The shared design tokens (below). |
| `/hub/assets/<name>` | The vendored brand marks (favicon, chip logo, mascot). |

The three app routes are **canonical without a trailing slash**, and `<page>/` answers `307` to `<page>` with the query string carried over ([#423](https://github.com/rtl-buddy/rtl_buddy/issues/423)). The redirect is not cosmetic: the SPA bundle is built with Vite `base: ''` — rtl-buddy-view needs relative asset references so its `embed.py` standalone HTML works over `file://` — and a browser resolves those against the *directory* of the current URL, so a page served at `/view/` asks for `/view/assets/…` and hangs on "Loading…". Canonicalising is what keeps one URL per asset instead of mounting the bundle at two depths. It is a temporary redirect rather than `301` because hub HTTP ports are pinned and reused across projects, and a cached permanent redirect against `127.0.0.1:<port>` would outlive the hub that issued it.

Cards advertise on **data presence**, the same rule `__RTL_BUDDY_GRAPH_URL__` follows: an app with nothing to show keeps its card, muted, carrying the command that would give it something (`rb graph build`, a coverage flag) rather than disappearing. An app whose origin already has a connected peer is badged **already open** — the hub allows one client per origin and a second tab supersedes the first, so the warning belongs before the click.

The landing page is deliberately **not** a hub peer: it polls `/hub/state.json` instead of opening `/ws`. A tab that only lists the apps must never hold an origin, or it would be the thing that evicted the app you had open.

### App names (display) and origins (wire)

The three browser apps are one family, and each carries two names:

| App | Long name | Short name | Wordmark | Wire origin |
|---|---|---|---|---|
| Schematic SPA (`/view`) | `rtl-buddy-schematic` | `sch` | `rtl-buddy-sch` | `view` |
| Knowledge graph pane (`/graph`) | `rtl-buddy-graph` | `gph` | `rtl-buddy-gph` | `graph` |
| Coverage pane (`/cov`) | `rtl-buddy-coverage` | `cov` | `rtl-buddy-cov` | `cov` |

The **long name introduces an app**: it is what the landing page's cards say, and what these docs say on first mention. Everything after that is the **short name** — every switcher link (`sch ↗`), every peer strip, every `send → gph` button, and the wordmark each pane titles itself with. Tooltips and prose use plain English instead ("the schematic", "the graph pane", "the coverage pane"): short names are labels, not sentences.

The **origin is the wire, and it does not move for a rename.** The schematic still registers as `view` and the graph pane as `graph`; `view_capture`, `resolve_signal_to_view`, the `/view` route, `view.json`, `--serve-viewer` and every Python identifier keep the names they have until a protocol v2 renames them in lockstep across both repos. The seam between the two vocabularies is a one-line **origin → display label** map — `{view: 'sch', graph: 'gph'}`, every other origin passed through unchanged — that each app applies wherever an origin would otherwise reach a user. It is hand-duplicated, because a pane is a self-contained single file by design: `graph_page.html`, `cov_page.html` and `landing_page.html` each carry it between `>>> origin-labels` markers, the SPA keeps its copy in `viewer/src/components/HubStatus.vue`, and the server-side names live in the `name`/`short` columns of `hub/landing_page.py`'s `APPS` table. Change one, change all of them. `rb hub status` is the deliberate exception: it lists raw origins, because it is the tool you reach for when you want to know what the *wire* says.

### Design tokens (`/hub/theme.css`)

One sheet, served same-origin, is the source of surfaces (`--bg` / `--panel` / `--panel-2` / `--line` / `--line-strong`), text tiers (`--fg` / `--fg-muted` / `--fg-faint`), one accent family (`--accent` / `--accent-contrast`), status colours and banner tints (`--ok` / `--warn` / `--err` / `--info` + `--ok-bg` / `--warn-bg` / `--err-bg`), the eight graph column hues (`--col-*`), the coverage ramp (`--cov-0` / `--cov-50` / `--cov-100` / `--cov-none` + `--tint-s` / `--cov-l` for the continuous `hsl(pct * 1.2, var(--tint-s), var(--cov-l))` fill), type (`--font-mono` / `--font-sans`, `--fs-base` 13px / `--fs-small` 11px), shape (`--radius-1/2/3`) and elevation (`--shadow-1/2`).

- **Light is the default.** Dark arrives via `@media (prefers-color-scheme: dark)`; `:root[data-theme="light"|"dark"]` wins in *both* directions for an app that pins a theme.
- Those two `[data-theme]` blocks are **generated** from the other two by `python -m rtl_buddy.hub.theme`, and `tests/test_hub_theme.py` fails when the checked-in file is not what the generator writes — three hand-maintained copies of one palette is three chances to drift. Edit the palette in `:root` or the dark media query, re-run the generator, commit.
- **Type rule: mono for data** (ids, paths, signals, numbers, machine values); sans is permitted for chrome and prose.
- Brand tokens (`--brand-ink`, `--brand-green`, `--brand-red`) are for identity marks only. They are never the interactive accent and never a status colour — the brand green is a saturated yellow-green that fails as text on white, which is why `--ok` stays its own green.
- Linking the sheet does not break the panes' offline rule: it is served by the same hub process, so a machine with no route off localhost still renders everything. Each pane keeps a short inline fallback for the tokens it cannot render without, in case an older hub serves a newer pane.
- **A `:root` fallback block goes *before* the `<link>`, never after.** Both selectors are `:root`, so they tie on specificity and document order decides the winner: a fallback placed after the link out-ranks the sheet permanently — including its `@media (prefers-color-scheme: dark)` values, so the page is stuck light while a `data-theme` pin (higher specificity) still appears to work. The alternative that is order-independent is the `var(--token, fallback)` form, which the viewer placeholder uses. `tests/test_hub_theme.py` enforces both the ordering and that the fallback values still match the sheet.

Artwork is deliberately minimal: a favicon on every hub page, a ~40px chip logo beside the landing wordmark, and at most one small (~120px) mascot on an empty-state panel. No hero images, no per-card art, no watermarks. The marks are vendored into `src/rtl_buddy/hub/assets/` (the art repo is private and panes must stay same-origin) at a few kB each.

### Hub chrome contract

Every hub app implements the same two strips, so moving between them costs nothing:

**Top bar**

| Position | Content |
|---|---|
| left | App identity — the app's name, and what it is showing (counts, active model). |
| centre | App-specific controls (search, toggles, reload). |
| right | App switcher: `⌂ hub` back to the landing, then the sibling apps. |

**Bottom status strip**

| Position | Content |
|---|---|
| left | Connection dot + status word. **One vocabulary: `connected` / `connecting…` / `offline`.** The dot is `--ok` / `--warn` / `--err` respectively. |
| middle | Peer list — who else is attached to this hub right now. |
| right | Message area, using the shared severity tokens (`--err` for errors, `--warn` for warnings, `--fg-muted` for notes). |

Detail that does not fit the vocabulary (the hub's `server_version`, the reason a socket dropped) belongs in the element's `title`, not in the status word — a strip that says four different things for "connected" is a strip nobody reads.

**One exception, and it is a control rather than a status word.** The hub allows [one client per origin](#peers-who-connects-to-the-hub), so a second tab of the *same* app evicts the first, and the hub tells the evicted tab so with an `error` envelope whose code is `superseded`. Retrying would only evict the tab the user just opened, so that tab stops reconnecting and its strip reads **"another *X* tab took this connection — click to take back"** on an `--err` dot. Clicking re-arms the connection with `takeover: true`, and the *other* tab receives the same treatment. Every other drop — hub restart, network — is an ordinary `offline` with the usual backoff.

That is also why a tab's *first* `hello` never sets `takeover`. It asks politely, and only if the hub answers `not_connected: <origin> client already registered` does it retry — once — with `takeover: true`. All three browser apps implement the identical flow: the SPA in `viewer/src/composables/useHub.js`, and each pane in its own file.

### Sending the selection to another app

The switcher opens a sibling app; it does not carry what you were looking at. Each pane therefore offers, beside the thing that is selected, **two controls per sibling app**:

- **`send → X`** — broadcast the selection in X's vocabulary and stay where you are. This is the whole cross-app interaction: opening an app fresh is the header switcher's job, and there is no `open ↗` variant in the rows (it was redundant with those links). Send first, then open X from the header, and the new tab still lands focused for free: the hub caches the latest `selection_changed`, `graph_focus` and `cov_focus`, and replays them to every client as it registers. No deep links and no extra wire types are involved.

Enablement follows the peer list in the status strip: `send` is dark when nobody is running X, because nothing would see the envelope (the tooltip points at the header links).

These are **broadcasts, not point-to-point sends**, and the vocabularies overlap on purpose: `graph_focus {node:"module:…"}` is understood by the graph pane *and* the schematic view, and `selection_changed` moves the coverage pane as well as the schematic. So a send aimed at one app may legitimately move another. Where two buttons emit the identical envelope, the tooltips say so rather than pretending the pair are independent.

## Peers (who connects to the hub)

| Peer | Transport | How it connects |
|---|---|---|
| **rtl-buddy-view SPA** (browser) | WebSocket `/ws` on the hub's `http_port` | Served at `/view` from the bundle when `rb hub start --serve-viewer` is in use. The bundle is injected with `window.__RTL_BUDDY_HUB__` at serve time. |
| **`rb wave` bridge** (`tools/wave_hub_bridge.py`) | Line-delimited JSON over TCP on `listen_port` | Started by `rb wave`; bridges surfer's WCP TCP socket to the hub. Reconnect with backoff. |
| **nvim plugin** ([`rtl-buddy-nvim`](https://github.com/rtl-buddy/rtl-buddy-nvim), installed by `rb nvim-install`) | Line-delimited JSON over TCP on `listen_port` | Auto-connects on startup (the managed setup calls `setup({ auto_connect = true })`). |
| **graph pane** (browser) | WebSocket `/ws` on the hub's `http_port` | The page the hub itself serves at `GET /graph` — see [Design knowledge graph pane](#design-knowledge-graph-pane). Needs no viewer bundle. |
| **coverage pane** (browser) | WebSocket `/ws` on the hub's `http_port` | The page the hub itself serves at `GET /cov` — see [Coverage pane](#coverage-pane). Needs no viewer bundle. |

The **landing page** at `/` is not in this table on purpose: it polls `/hub/state.json` and never registers an origin, so it cannot evict an app you have open.

`rb hub status` lists every origin a user can have open — `view`, `wave`, `src`, `graph`, `cov` — as `CONNECTED` or `not connected`. `cli` is excluded (it is the status query itself) and so is `notebook` (it peers for one marimo session rather than being an app you keep open).

Each peer has a closed `Origin` enum value — a **wire** value, unaffected by the [display names above](#app-names-display-and-origins-wire): `view` (the schematic SPA, shown as `sch`), `wave` (the `rb wave` surfer bridge), `src` (editor adapters — the nvim plugin registers as `src`), `cli` (`rb hub send`), `notebook` (the axi-profiler marimo notebook, added so it can peer over the event broker), `graph` (the knowledge graph pane, shown as `gph`) and `cov` (the coverage pane). The hub allows at most one client per origin; a second `hello` for an already-registered origin is refused unless it sets `takeover: true`, in which case the older peer is evicted (`bye`-broadcast and its socket closed) — used by a new SPA tab to take over from a stale one. The graph and coverage panes have their own origins rather than sharing `view` precisely because of that rule: they are meant to be open *alongside* the schematic, since clicking a module in the graph — or a cold line in the coverage pane — selects it in the schematic.

## Protocol

Wire envelope is line-delimited JSON, one record per line, UTF-8. The full spec lives in [rtl-buddy/rtl-buddy-view#19](https://github.com/rtl-buddy/rtl-buddy-view/issues/19); the JSON Schema enforcing it ships at `src/rtl_buddy/hub/schema/hub-protocol-v1.json`. Encoded and decoded by `rtl_buddy.hub.protocol`, which validates on both sides — unknown fields are caller bugs, not forward-compat points.

State events (selection_changed, signal_selected, cursor_moved, …) are broadcast to every connected peer **except** the origin. Requests (`resolve_*`, `goto_declaration`, …) are routed to the peer whose origin owns the target coordinate system; if no peer is registered for that origin, the hub replies with `error{code: "not_connected"}`. The `view ↔ wave ↔ src` resolver lives in `rtl_buddy.hub.resolver` and consumes the `view.json` snapshot pointed at by `mapping.view_json`.

Lifecycle events (`hello` / `welcome` / `peer_joined` / `bye`) keep each peer's view of the registry live without re-fetching: `welcome` carries the snapshot at handshake time, and `peer_joined` / `bye` are deltas the hub broadcasts when later peers connect or disconnect. The joining or leaving peer's origin is in the envelope's `origin` field (payload is empty). Consumers should react to all three to maintain a current peer list — relying on `welcome` alone leaves the list frozen at handshake time.

The hub also **augments `source_focused`**: when a `src` peer (e.g. nvim's `:RtlBuddyShow`) broadcasts `{file, line, col}`, the resolver looks up the instance(s) whose `source` range in `view.json` contains the point and the hub emits a derived `selection_changed { instance_path: [...] }` with `origin: "cli"`. The schematic SPA already handles `selection_changed` — pan/highlight the matching instance — so this bridge makes editor cursor movement light up the schematic without a SPA-side protocol change. Multiple matches (nested instances) come back smallest-range first; consumers picking element `[0]` get the most-specific instance. Line-only matching is used for multi-line ranges (cursor at column 1 still finds an instantiation whose keyword sits further right); single-line ranges still use columns so two instantiations on the same line resolve distinctly.

The hub also relays a **`diagnostics_set`** event for lint and analysis findings to the SPA's on-canvas badge layer. Each `diagnostics_set` carries a producer `source` key (latest-writer-wins per source, so re-publishing replaces that source's set), a list of `{file, line, severity, code, message}` items, and an optional `instance_path` per item (a fast path for the SPA badge layer that skips the file+line resolver). `rb hub send diagnose SOURCE ITEM…` (with `--clear` / `--instance`) lets any tool push diagnostics. A **`wave_values_changed`** event is emitted on `cursor_moved` so the SPA can show signal values at the cursor.

`GET /healthz` returns `ok` for liveness probes.

## Driving the hub from the CLI (`rb hub send`)

`rb hub send` is a one-shot peer: it connects to the running hub as `origin=cli`, sends one request or state event, prints any reply, and disconnects. It exits with code 2 when no hub is running (or `$RTL_BUDDY_HUB` is unset). It is the scripting/automation entry point and the easiest way to poke the hub by hand.

The verbs group into broadcast, wave-control, SPA, source, and resolve families (see the [CLI reference](../reference/cli.md#hub-send) for the full flag list of each):

- **State broadcast:** `select INSTANCE_PATH`, `signal SIGNAL`, `cursor T_FS`, `scope WAVE_SCOPE`, `open FILE:LINE[:COL]`.
- **Wave control** (routed to surfer via the `rb wave` bridge): `wave-add VARIABLES…`, `wave-cursor T_FS`, `wave-scope WAVE_SCOPE`, `wave-pan T_FS`, `wave-zoom START_FS END_FS`, `wave-zoom-fit`.
- **Wave-view item management** (also via the `rb wave` bridge — lets an agent curate the signal list): `wave-items` (list the displayed items as `{id, type, name}`), `wave-remove IDS…` (reports `removed` vs `not_found`), `wave-move IDS… --to N | --before ID` (reorder), `wave-comment TEXTS… [--after ID]` (add comment rows / dividers, returns their ids). Every verb reports genuine success/error: a surfer-side rejection (unknown id, illegal move, unknown scope) comes back as a hub `error` and a non-zero CLI exit, not a false `{"ok": true}`. `wave-move`/`wave-comment` need the [`rtl-buddy/surfer`](https://github.com/rtl-buddy/surfer) fork with the `move_items` / `add_dividers` WCP commands.
- **SPA:** `view-pan INSTANCE_PATH`, `overlay NAME --on/--off` (`clock` / `reset` / `axi-perf` / `wave`), `capture --out PATH [--format png|svg] [--scale …]`.
- **Source:** `open-source FILE:LINE[:COL]`.
- **Diagnostics:** `diagnose SOURCE ITEM…` (each `ITEM` is `file:line:severity:code:message`; `--clear`, `--instance`).
- **Coverage pane:** `cov-focus TARGET [--metric …] [--line N] [--item NAME]` — focus the [coverage pane](#coverage-pane) on one target of the run's model. `TARGET` is prefixed (`file:design/blk.sv`, `module:blk`, `test:verif/blk#basic`); an unprefixed string is read as a file path. Broadcast and cached like `graph-focus`, hints included — a replay that kept only the target would silently downgrade "this branch, on line 84" to "this file".
- **Graph pane:** `graph-focus NODE` — focus the [design knowledge graph pane](#design-knowledge-graph-pane) on one `graph.json` node id (`module:fifo`, `test:verif/dma#smoke`, `covitem:dma#DMA-COV-1`, …). Broadcast, and cached by the hub, so sending it before the browser tab is open still lands: the focus is replayed to the pane when it registers.
- **State / resolve:** `state` (snapshot of active model / selection / cursor / scope / peers), and `resolve {view-to-wave|wave-to-view|signal-to-view}`.

## Design knowledge graph pane

`rb hub start --serve-viewer` also serves the [design knowledge graph](graph.md) as an interactive page at `GET /graph`, next to the schematic rather than instead of it. Two routes:

- `GET /graph.json` — `artefacts/graph/graph.json` joined with `artefacts/graph/results-overlay.json` **in memory**, using the same `annotate_graph()` and `annotate_coverage()` joins the query verbs use. `graph.json` on disk is never written: hash stability across regressions is why the overlay is a separate file to begin with. The body is the node-link envelope with each test node carrying its `results` entry, each node the [coverage join](graph.md#coverage-on-the-graph) knows carrying its `coverage` entry, each node carrying the `category` column it renders in, plus a `graph.hub` block (where the two files were read from, node/link counts, per-tier and per-column counts, the column order, the overlay's status summary, and the coverage run's header minus its per-node map) so the page can render a header and a legend without a second round-trip. Read per request, so `rb graph build` / `rb graph results` in another terminal shows up on **reload**. Returns 404 with a JSON `error` naming `rb graph build` when there is no graph yet.
- `GET /graph` — the page. One HTML document with no build step, no CDN and no web font, because the hub is routinely run on machines with no route off localhost; its only external references are same-origin hub routes (`/hub/theme.css`, `/hub/assets/*`), and it carries an inline fallback for the tokens it cannot render without. Nodes are laid out in [flow columns](graph.md#looking-at-the-graph) (`spec` → `design` → one per verification flow) with a small force relaxation inside each, one colour per column, and test nodes get a pass/fail ring from the overlay. A **coverage** toggle appears when the overlay carries a coverage join and repaints the design column from the shared [coverage ramp](#design-tokens-hubthemecss) — see [Coverage on the pane](graph.md#coverage-on-the-pane). It is served even with no graph built — its empty state names `rb graph build`, which is more useful than a blank tab.

Clicking a node sends the same envelopes the SPA sends, over the same `/ws`:

- **`selection_changed`** for anything that resolves to an instance path in the schematic. An `instance` node's id already *is* the coordinate (`inst:<top>/<dot.path>` — the resolver's identity); a `module` node has no instance path of its own, so the pane picks the shallowest instance of it, which is what a person means by "show me this module".
- **`open_source`** (routed to the `src` peer, i.e. nvim) for any node that knows its `file`, at its `line`.

Both are individually toggleable in the pane's toolbar. The reverse direction works too: `rb hub send graph-focus NODE` centres and selects a node, and a `selection_changed` from the SPA or the editor highlights the matching instance node in the graph.

The inspector heads the selected node with the [cross-app row](#sending-the-selection-to-another-app): `send → sch` re-sends the coordinate above *without* the `sync schematic` toggle having to be on, and `send → cov` translates the node into a `cov_focus` target — a `test:` node to that test's attribution, a `model:`/`module:` node to the module, a spec `coverage_item` to its block with the cover column up and the item named, and anything else that knows its `file` to that file and line. A node that is none of those leaves both buttons dark and says why.

When a graph exists, the index page also gets a `window.__RTL_BUDDY_GRAPH_URL__ = "/graph.json"` injection alongside `__RTL_BUDDY_VIEW_URL__`, so an SPA overlay can advertise the pane on presence of the global instead of probing the endpoint and handling a 404.

## Coverage pane

`rb hub start --serve-viewer` also serves the run's [coverage](coverage.md) as an interactive page at `GET /cov`, in the same mould as the graph pane. Three routes:

- `GET /cov.json` — the newest `cov_dir/manifest.json` and the model it points at, assembled by **the same builder `rb cov summary` uses** (`rtl_buddy.cov.query.detail_payload`), so the pane and the CLI can differ in presentation but never in numbers. It is the summary payload with `files` deepened to carry every point and its per-test attribution, plus a `hub` block (schema version, the model path, the metric order, the source route). Read per request, so a regression finishing in another terminal shows up on **reload**. Returns 404 with a JSON `error` naming the command that produces coverage when there is none.
- `GET /cov/source?path=…` — one file's text, one entry per line. Not folded into `/cov.json`: a model on a real design names hundreds of files and inlining them all would send tens of megabytes to render one. `path` must name a file the model itself lists, and is resolved under the project root and nowhere else — the argument comes from a query string, and a browser tab is reachable by anything that can reach the port, so the grant is the file set `/cov.json` already handed the pane rather than anything readable under the root — and a file over the annotation limit is refused with a reason rather than hanging the tab.
- `GET /cov` — the page. One HTML document with no build step, no CDN and no web font; its only external references are same-origin hub routes (`/hub/theme.css`, `/hub/assets/*`), and it carries an inline fallback for the tokens it cannot render without. It is served even with no coverage collected — its empty state names the command, which is more useful than a blank tab.

What it shows: a dashboard (per-metric scalars from the shared [coverage ramp](#design-tokens-hubthemecss), the run's provenance, observed SVA cover points), a file list with module and path filtering, and per-file **source annotation** — one column per metric (`L B T E C`), always, under a sticky header carrying that file's totals for each: `L 100% 22/22`, `B 96% 75/78`, `—` for a metric the file never collected. `L` is the hit-count gutter itself; the rest carry each line's points. The **metric picker drives the file list only** — its bars, its coldest-first ranking and the cold-only filter — and the pane opens on `toggle`, where the holes usually are, falling back to the first metric the run actually collected. Clicking a column header sets it too. The ranking is lowest ratio, then most absolute misses, the same rule `rb cov summary` uses, with files that have no points of the metric last, since those are silent rather than cold. Every number is **merged across all tests** unless you say otherwise: picking a test in the tests table turns it into a lens, and every number becomes that test's contribution, which is how you answer "what would I lose by dropping it". The table's pinned first row, *all tests (merged)*, is the way back — as is the `lens: <test> ×` pill shown beside the file's totals and in the detail panel, and clicking the selected test again.

A cell is one small badge — `12/16` — tinted on the same ramp, because one 32-bit bus declaration is 64 toggle points and a chip each pushed the code off the right of the screen. A line carrying a single named point of that metric shows the name instead. Every column is capped, so the code stays where you can read it.

Clicking a badge in any column opens the **detail panel**: one panel — keyed on the (line, metric) pair the badge belongs to — docked at the bottom of the file view above the status strip, *outside* the code's scroller — a badge on the last visible line of a long file would otherwise open its detail below the fold. It is headed `apb_intf.sv:19 · T 6/16`, capped at 40% of the window with its own scroll, and closed by its `×` or by clicking the same badge again; the line it belongs to stays marked in the code while it is up, and a lens change updates its counts without closing it. Toggle points in it are drawn as a **bit grid**: one square per bit of each signal, MSB left, rows of 32 with a gap at every byte, the top half of a square being `0→1` and the bottom half `1→0`. A bit covered both ways is a solid block, a half-toggled bit is visibly half, and hovering names the bit and both hit counts under the active lens. Clicking a square shows that bit's per-test attribution in the same panel, under the grid you clicked it in — the uncovered direction when only one of the two is uncovered, since that is the one you came to look at. A `cov_focus` whose `item` names a toggle point (`paddr[3]:0->1`) opens the panel and highlights its square. It needs no `metric` hint to do it: every metric has a column, so the point's own name says which cell it lives in.

The hit-count column opens the same panel, for the same reason: hovering it lists the per-test hits as a peek, and clicking it docks that line's attribution — headed `apb_intf.sv:19 · line · 322 hits` — with the same close-and-replace semantics. That click is not the row's click: inspecting a line's attribution never emits `source_focused` or `open_source`, so reading coverage does not move anybody's editor.

Clicking a line sends the same envelopes the other panes send, over the same `/ws`:

- **`source_focused`** `{file, line, col}`. Not `selection_changed`: this pane knows files and modules, never instance paths. The hub's resolver already turns a file+line into the instance(s) whose source range contains it and broadcasts the derived `selection_changed` itself (see [Protocol](#protocol)) — which is the only way to reach the schematic without inventing a coordinate the coverage model does not have.
- **`open_source`** (routed to the `src` peer, i.e. nvim) at the clicked line.
- **`graph_focus`** `{node: "module:<name>"}` when you click a module chip, since `module:<name>` is the id that module carries in the graph.

The file header carries the [cross-app row](#sending-the-selection-to-another-app) beside those chips, for the open file's first module: `send → gph` and `send → sch` both broadcast that one `graph_focus`, since the graph pane and the schematic read the same `module:` vocabulary — the two buttons differ only in which tab they assume or open, and their tooltips say so.

Both directions work: `rb hub send cov-focus <target>` focuses the pane (replayed on connect, so it lands even before the tab is open), an editor's `source_focused` scrolls it to the matching file and line, and a `selection_changed` from the SPA is matched to a module by the usual instance-prefix convention (`u_`, `i_`, `inst_`, `dut_`) — a soft miss when the convention does not hold, since nothing in either model says which module an instance is of. A `test:` target is matched on the model's bare test name, and a qualified `test:<suite>#<name>` — the form the schema and `rb hub send cov-focus` document — falls back to its `#` fragment.

When a coverage manifest exists, the SPA index also gets a `window.__RTL_BUDDY_COV_URL__ = "/cov.json"` injection, the same presence-advertisement `__RTL_BUDDY_GRAPH_URL__` uses. Discovery is a bounded walk rather than one `stat` (coverage artefacts land wherever the command ran), so the answer is cached for a few seconds — a run finishing elsewhere shows up on the landing page's next poll but one.

## Auto-start on macOS (LaunchAgent)

On macOS, `rb hub install-launchagent` writes `~/Library/LaunchAgents/com.rtl-buddy.hub.plist` (with `RunAtLoad` + `KeepAlive`) and `launchctl load`s it, so the hub starts at login and restarts if it dies. The agent runs `rb hub start --foreground` from the project directory and routes stdout/stderr to `.rtl-buddy/hub.log`. `rb hub uninstall-launchagent` unloads and removes the plist. On non-macOS platforms both commands error with `LaunchAgentUnsupportedError`.

## AXI-perf overlay and notebook spawning

When the hub is started with `rb hub start --serve-viewer --axi-perf-from <axi-perf.json>` (the file produced by `rb axi-profile run`), it bakes a per-bundle / per-interconnect throughput overlay into every generated `view.json` and records the source test + suite dir so the SPA's "Open in marimo" button can launch the matching notebook without re-prompting. Point `--axi-perf-from` at the canonical `<suite>/artefacts/axi/<test>/axi-perf.json` so that derivation works. The file's existence is checked up-front (a missing path is a clean start-up error).

The SPA and a deep-dive marimo notebook stay in sync through an in-memory **event broker** exposed as a WebSocket at `GET /api/events/sync` (opaque-string pub/sub: the broker relays each inbound message to every *other* connected client; topic routing and echo-suppression live in the clients; a slow client's outbound queue is bounded and drops the oldest message on overflow). `GET /api/axi-profile/notebook?test=NAME&suite_dir=PATH` spawns `rb axi-profile notebook --headless` on demand, with marimo-session reuse and shutdown cleanup. The hub injects `RB_HUB_EVENTS_URL=ws://127.0.0.1:<http_port>/api/events/sync` into the spawned notebook so it joins the broker as a peer with `origin=notebook`; SPA bundle-node clicks then drive the live notebook.

## Troubleshooting

**`rb hub start` exits with "already running"** — `.rtl-buddy/hub.json` exists and its PID is live. If the prior daemon really is gone, the file is stale (clean shutdown didn't run); delete it and retry. `rb hub status` distinguishes the two cases.

**Port already in use** — pin `listen_port` (and `http_port` if using `--serve-viewer`) to a free port in `hub.toml`, or leave them at `0` to let the OS pick. The chosen port lands in `hub.json` either way.

**Peer can't find the hub from outside the project tree** — set `RTL_BUDDY_HUB=<host>:<port>` (the `tcp` value from `.rtl-buddy/hub.json`, e.g. `RTL_BUDDY_HUB=127.0.0.1:53201`) in the peer's environment. The default discovery walks up from `cwd` looking for `.rtl-buddy/hub.json`, which doesn't work for processes launched from elsewhere. The override is a `host:port` string, not a file path.

**`rb wave` bridge reports surfer disconnected** — the bridge owns the WCP TCP connection, not the hub. Check the surfer fork is on PATH and built with WCP support; see [Waveform Viewer](wave.md). The hub stays up regardless; reconnect is automatic.

**`rb hub config validate` reports "unknown section"** — typo. The schema accepts exactly `[hub]` and `[mapping]`; everything else (surfer flags, nvim keymaps, …) belongs in the adapters' own config.

**Hub log empty or absent** — `rb hub log` tails `.rtl-buddy/hub.log` by default. The `[hub].log_path` setting controls the location; logs route through `log_event()` like the rest of `rtl_buddy`, so `--machine` mode produces JSON Lines.

## Writing a new adapter

Bring up a TCP client against the hub's `listen_port`, send the `hello` envelope claiming an origin, accept the `welcome` reply, then send / receive state events and requests per [rtl-buddy-view#19](https://github.com/rtl-buddy/rtl-buddy-view/issues/19). The JSON Schema at `src/rtl_buddy/hub/schema/hub-protocol-v1.json` is the contract — validate against it on both sides and unknown `type` strings should be silently dropped (forward-compat rule from §11 of the spec).

The existing peers — `tools/wave_hub_bridge.py` and the [`rtl-buddy-nvim`](https://github.com/rtl-buddy/rtl-buddy-nvim) plugin — are the reference adapters. Both stay narrow on purpose: parse the envelope, translate to the peer's native API, route, repeat.

## Reference

- Wire protocol spec: [rtl-buddy-view#19](https://github.com/rtl-buddy/rtl-buddy-view/issues/19)
- JSON Schema: `src/rtl_buddy/hub/schema/hub-protocol-v1.json`
- Implementation: `src/rtl_buddy/hub/`
- Wave bridge: `src/rtl_buddy/tools/wave_hub_bridge.py`, `src/rtl_buddy/tools/wave_launcher.py`
- nvim plugin: [`rtl-buddy-nvim`](https://github.com/rtl-buddy/rtl-buddy-nvim) (installer: `src/rtl_buddy/tools/nvim_install.py`, command `rb nvim-install`)
