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
