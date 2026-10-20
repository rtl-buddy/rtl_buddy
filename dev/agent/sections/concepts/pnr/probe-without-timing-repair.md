## Probe without timing repair

Set `mode: probe` on a run to find out quickly whether a floorplan places and routes:

```yaml
runs:
  - name: blk_probe
    # ...
    mode: probe        # default closure
```

A probe keeps `repair_design`, `repair_tie_fanout` and every legalization, skips the post-CTS setup and hold repair and the global-route repair, and stops after global route as [`--stop-after global_route`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stop-after-a-stage) does: it writes the `global_route` checkpoint and no routed DEF or ODB. The result is a `PASS` with `routed: false`, `mode: probe` and `stopped_after: global_route`, with the global-route overflow and the timing on the global-route estimate. Like any stopped run it is no verdict on a routed design, so `xfail:` turns neither its `PASS` into `XPASS` nor a `FAIL` into `XFAIL`. `post-route-repair` does not apply, and `--stop-after` may name an earlier stage. A probe publishes no abstract, so a run with `harden: true` and `mode: probe` fails to load. A closure run cannot [resume](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#resume-a-staged-run) a probe's checkpoints, whose stages skipped timing repair; run the closure from the start.
