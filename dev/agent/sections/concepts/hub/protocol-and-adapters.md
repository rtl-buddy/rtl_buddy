## Protocol and adapters

The protocol is UTF-8 line-delimited JSON over TCP or WebSocket. A peer sends `hello`, receives `welcome`, and tracks `peer_joined` and `bye`. State events go to every peer except their origin. Requests go to the origin that owns the target coordinate system; an absent target returns `not_connected`. `GET /healthz` is the liveness endpoint.

The JSON Schema is `src/rtl_buddy/hub/schema/hub-protocol-v1.json`, a vendored copy of [`rtl-buddy-sch/schemas/hub-protocol-v1.json`](https://github.com/rtl-buddy/rtl-buddy-sch/blob/main/schemas/hub-protocol-v1.json). Do not edit the copy; re-copy it from `rtl-buddy-sch`. A new adapter should validate envelopes against the schema and follow `src/rtl_buddy/tools/wave_hub_bridge.py`.
