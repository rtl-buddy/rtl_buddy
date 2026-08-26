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
