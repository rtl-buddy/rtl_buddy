## Repair timing after CTS

After CTS the flow repairs setup (unless `post-cts-setup-repair: false`), then hold, on placement parasitics. `post-cts-repair` sets their margins in ns; the [YAML reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#synthesis-and-physical-design-tools) says how each is passed.

```yaml
    post-cts-repair:
      setup-margin: 0.05
      hold-margin: 0.01
```

- The setup margin also goes to the hold repair, which then keeps that much setup slack, on placement parasitics, on the paths it delays.
- Set it when post-CTS hold buffers leave setup-critical paths failing after routing, since later repairs do not remove them.

`max-buffer-percent`, in `post-cts-repair`, `global-route-repair` or `post-route-repair`, goes to that block's `repair_timing -hold` as `-max_buffer_percent`. It caps the buffers hold repair inserts, as a percentage of the design's instances. Unset keeps OpenROAD's default of 20%, as ORFS does. Raise it when hold repair stops with `RSZ-0060` (max buffer count reached), such as on a block whose input registers need padding on most bits.
