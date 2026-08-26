## Protocol and adapters

The protocol is UTF-8, line-delimited JSON over TCP or WebSocket. Its JSON Schema is `src/rtl_buddy/hub/schema/hub-protocol-v1.json`.

After connecting, a peer sends `hello`, receives `welcome`, and tracks `peer_joined` and `bye` updates. State events are broadcast to every peer except their origin. Requests are routed to the origin that owns the target coordinate system; an absent target returns `not_connected`.

The hub augments `source_focused` with resolved `selection_changed` events and relays producer-scoped `diagnostics_set` updates. `GET /healthz` is the liveness endpoint.

For a new adapter, validate envelopes against the schema and use `src/rtl_buddy/tools/wave_hub_bridge.py` as the narrow reference: connect, translate to the peer API, route, and reconnect.
