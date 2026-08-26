## Peers (who connects to the hub)

| Peer | Transport | How it connects |
|---|---|---|
| **rtl-buddy-view SPA** (browser) | WebSocket `/ws` on the hub's `http_port` | Loaded from the bundle when `rb hub start --serve-viewer` is in use. The bundle is injected with `window.__RTL_BUDDY_HUB__` at serve time. |
| **`rb wave` bridge** (`tools/wave_hub_bridge.py`) | Line-delimited JSON over TCP on `listen_port` | Started by `rb wave`; bridges surfer's WCP TCP socket to the hub. Reconnect with backoff. |
| **nvim plugin** (`src/rtl_buddy/nvim/rtl_buddy_wave.lua`) | Line-delimited JSON over TCP on `listen_port` | Connects when the user opens a file rtl-buddy knows how to resolve. |

Each peer has a closed `Origin` enum value: `view` (the SPA), `wave` (the `rb wave` surfer bridge), `src` (editor adapters — the nvim plugin registers as `src`), `cli` (`rb hub send`), and `notebook` (the axi-profiler marimo notebook, added so it can peer over the event broker). The hub allows at most one client per origin; a second `hello` for an already-registered origin is refused unless it sets `takeover: true`, in which case the older peer is evicted (`bye`-broadcast and its socket closed) — used by a new SPA tab to take over from a stale one.
