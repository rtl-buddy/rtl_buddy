## Probe a floorplan

`mode: probe` checks quickly whether a floorplan places and routes.

- It skips setup and hold repair (floorplan, post-CTS and global-route) and the antenna repair after global route, stops after global route, and writes the `global_route` checkpoint.
- It passes with `routed: false`, the global-route overflow, and timing on the global-route estimate.
- `xfail:` does not apply, and a `closure` run cannot resume from its checkpoints.
- It publishes no abstract, so `harden: true` with `mode: probe` fails to load.
