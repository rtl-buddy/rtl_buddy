## Writing a new adapter

Bring up a TCP client against the hub's `listen_port`, send the `hello` envelope claiming an origin, accept the `welcome` reply, then send / receive state events and requests per [rtl-buddy-view#19](https://github.com/rtl-buddy/rtl-buddy-view/issues/19). The JSON Schema at `src/rtl_buddy/hub/schema/hub-protocol-v1.json` is the contract — validate against it on both sides and unknown `type` strings should be silently dropped (forward-compat rule from §11 of the spec).

The existing peers — `tools/wave_hub_bridge.py` and `src/rtl_buddy/nvim/rtl_buddy_wave.lua` — are the reference adapters. Both stay narrow on purpose: parse the envelope, translate to the peer's native API, route, repeat.
