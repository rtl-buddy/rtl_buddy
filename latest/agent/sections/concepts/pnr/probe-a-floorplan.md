## Probe a floorplan

`mode: probe` on a run checks quickly whether a floorplan places and routes. It skips setup and hold repair and the antenna repair after global route, stops after global route and writes the `global_route` checkpoint. The result is a `PASS` with `routed: false`, the global-route overflow and timing on the global-route estimate. `xfail:` does not apply to it, and a `closure` run cannot resume from a probe's checkpoints. A probe publishes no abstract, so `harden: true` with `mode: probe` fails to load.
