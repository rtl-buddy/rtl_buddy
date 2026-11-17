## Repair timing after CTS

After CTS the flow repairs setup, unless `post-cts-setup-repair` is false, then hold, on placement parasitics. `post-cts-repair` sets the margins of that repair, in ns:

```yaml
    post-cts-repair:
      setup-margin: 0.05
      hold-margin: 0.01
```

`setup-margin` goes to `repair_timing -setup`, and `hold-margin` and a set `setup-margin` go to `repair_timing -hold`, as in the repairs after routing and in ORFS. Unset or 0, a margin is not passed, as OpenROAD's default is 0. The setup margin keeps that much setup slack, on placement parasitics, on the paths the hold repair delays: set it when the hold buffers the post-CTS repair inserts leave setup-critical paths failing after routing, since later repairs do not remove them. With `post-cts-setup-repair: false` the setup repair does not run, and a `setup-margin` still goes to the hold repair. A run's block overrides the platform's field by field, and `null` clears the margins. A probe skips the post-CTS repair.

`max-buffer-percent` in `post-cts-repair`, `global-route-repair` or `post-route-repair` goes to that block's `repair_timing -hold` as `-max_buffer_percent`: the most buffers hold repair inserts, as a percentage of the design's instances. Unset leaves OpenROAD's default of 20%, which ORFS also keeps. Raise it when hold repair stops with `RSZ-0060` (max buffer count reached), for example on a block whose input registers need padding on most bits.
