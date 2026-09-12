## Protocol and adapters

The protocol is UTF-8, line-delimited JSON over TCP or WebSocket. Its JSON Schema is `src/rtl_buddy/hub/schema/hub-protocol-v1.json` — a **vendored copy**. The contract is owned by [`rtl-buddy-sch/schemas/hub-protocol-v1.json`](https://github.com/rtl-buddy/rtl-buddy-sch/blob/main/schemas/hub-protocol-v1.json); re-copy it byte-for-byte rather than editing ours.

The `origin` vocabulary in that schema is hand-copied into `hub/protocol.py`'s `Origin` enum, which `tests/test_hub_protocol.py::test_origin_enum_matches_vendored_schema` pins to the vendored file — without it a re-sync that adds a peer passes schema validation and then raises `ValueError` in `decode()` on that peer's first envelope. Adding an origin is a lockstep edit across three repos in a fixed merge order (schema first, this repo last); the checklist, naming the test that catches each missed copy, is [`docs/hub-protocol.md` §13](https://github.com/rtl-buddy/rtl-buddy-sch/blob/main/docs/hub-protocol.md#13-adding-or-renaming-an-origin--lockstep-checklist) in that repo.

After connecting, a peer sends `hello`, receives `welcome`, and tracks `peer_joined` and `bye` updates. State events are broadcast to every peer except their origin. Requests are routed to the origin that owns the target coordinate system; an absent target returns `not_connected`.

The hub augments `source_focused` with resolved `selection_changed` events and relays producer-scoped `diagnostics_set` updates. `GET /healthz` is the liveness endpoint.

For a new adapter, validate envelopes against the schema and use `src/rtl_buddy/tools/wave_hub_bridge.py` as the narrow reference: connect, translate to the peer API, route, and reconnect.
