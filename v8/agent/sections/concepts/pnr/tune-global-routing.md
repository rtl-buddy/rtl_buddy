## Tune global routing

The `global-route` block on the platform or run sets `global_route` options; a run's block overrides the platform's field by field. The [YAML reference](https://rtl-buddy.github.io/rtl_buddy/v8/reference/yaml/#synthesis-and-physical-design-tools) gives each key and the calls it reaches.

```yaml
global-route:
  congestion-iterations: 100
  congestion-report-iter-step: 25
  grid-origin: [3, 3]                       # DBU
  layer-adjustment: {met1: 0.9, met2: 0.8}
```

| Problem | Key |
| --- | --- |
| Overflow that more rip-up and reroute would clear | `congestion-iterations` (default 30), with `congestion-report-iter-step` to watch it fall. |
| Long wires low on the stack, or one congested layer | `layer-adjustment` per layer, applied after `routing-layer-adjustment`. |
| Boundary pins centred on a GCell edge fail detailed route (ASAP7 pins on a fine lattice) | `grid-origin`. |
| Critical nets on resistive layers | `critical-nets-percentage`, `resistance-aware`. |
| Seeing where a design overflows without failing at global route | `allow-congestion: true`. |

OpenROAD drops `grid-origin` on every call, so rb passes it to each, including the incremental routes of both repairs. OpenROAD 26Q4 offsets only the guides, not the GCell grid, so rb rebuilds the grid from the guides before each detailed route. It does so after the `global_route` checkpoint, so a staged or resumed detailed route starts from the router's own route, as a one-session run does.

`allow-congestion` is for diagnosis: global route keeps its overflow and the flow continues to detailed route. The result is a `PASS` qualified `global-route.allow-congestion (diagnosis only)` with `allow_congestion: true` and `global_route_overflow`, never a clean pass. Such a run cannot be `harden: true`.
