## Corners share one SDC mode on an OpenROAD without modes

`rb pnr` gives each corner its own SDC mode only on an OpenROAD that can switch back to an earlier mode, 26Q4 or newer. On an older one, such as 26Q2, every corner is timed in one mode and rb warns `pnr.corner_modes_unsupported`:

- A generated clock's latency comes from one corner at every corner, so a forwarded clock can be timed for hold at a fast corner on a slow corner's latency.
- A per-corner `constraints:` fails setup with `pnr.corner_constraints_unsupported`.
- `STA-1140 library ... already exists` is logged once per extra corner for a macro with one Liberty. Per-corner Liberty files of one macro that share a `library` name log it in every session, with or without modes; it is harmless there, because each file is named only by its own corner's scene.
- Every `<top>.<corner>.routed.sdc` holds that one mode, and checkpoints write no per-corner SDC.

Upgrade OpenROAD, or run each corner on a single-corner platform. See [Sign off at several corners](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#sign-off-at-several-corners).
