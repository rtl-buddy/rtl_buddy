## Peers (who connects to the hub)

| Peer | Transport | How it connects |
|---|---|---|
| **rtl-buddy-view SPA** (browser) | WebSocket `/ws` on the hub's `http_port` | Loaded from the bundle when `rb hub start --serve-viewer` is in use. The bundle is injected with `window.__RTL_BUDDY_HUB__` at serve time. |
| **`rb wave` bridge** (`tools/wave_hub_bridge.py`) | Line-delimited JSON over TCP on `listen_port` | Started by `rb wave`; bridges surfer's WCP TCP socket to the hub. Reconnect with backoff. |
| **nvim plugin** (`src/rtl_buddy/nvim/rtl_buddy_wave.lua`) | Line-delimited JSON over TCP on `listen_port` | Connects when the user opens a file rtl-buddy knows how to resolve. |

Each peer has a closed `Origin` enum value (`view`, `wave`, `nvim`, …); the hub allows at most one client per origin in v1.
