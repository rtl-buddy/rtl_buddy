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
- `view_changed` event — broadcast on every active-model change. Envelope:
  ```json
  {"v":1, "id":"…", "origin":"cli", "kind":"event", "type":"view_changed",
   "payload":{"model":"ip_dtnpu_dma", "models_file":"/abs/path/to/models.yaml",
              "view_url":"/view.json?model=ip_dtnpu_dma"}}
  ```
  Sent to every connected client (SPA tabs, nvim, `rb wave` bridge) so they can refresh model-scoped state.

The active model is also recorded in `.rtl-buddy/hub.json` under `active_model` (optional field) and surfaced in `rb hub status` output.
