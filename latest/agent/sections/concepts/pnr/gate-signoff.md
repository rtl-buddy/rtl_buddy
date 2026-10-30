## Gate signoff

A `signoff:` block on the platform or the run names the corners each timing side is signed off at and the checks that fail a routed run:

```yaml
    signoff:
      setup-corners: [ss]                  # default: every corner
      hold-corners: [ff]                   # default: every corner
      fail-on: [timing, drc, electrical]   # default: none
```

- `timing` fails when setup WNS over `setup-corners` or hold WNS over `hold-corners` is negative or was not reported. The final reports print two digits, so a WNS of `-0.00` is negative and `0.00` passes. Setup with no constrained path at every one of its corners fails, since nothing was checked; hold in that case passes.
- `drc` fails when `drc_count` is above 0, or when the detailed route wrote no DRC report (`drc.count` is then null). A clean route writes an empty report.
- `electrical` fails on any max-slew, max-capacitance or max-fanout violator, or when one of the three counts was not reported. `fail-on-electrical: true` adds this gate to `fail-on`, with or without the block.

A run's block overrides the platform's field by field, and `null` sets a key to its default or turns the block off. A corner the platform does not analyse fails the platform's load, or the run's setup.

The result's `signoff` field gives each gate its values and a `verdict` of `pass`, `fail` or `not_gated`: `timing.setup` and `timing.hold` with `corners`, `wns_ps`, `worst_corner` and `unconstrained`, `drc.count`, and the three electrical violator counts. `wns_setup_ps` and `wns_hold_ps` stay the worst over every corner. A failed gate fails the run without `fail_stage`, so `xfail:` can excuse it, and the description names each gate, such as `signoff failed: timing (setup WNS -0.1200 ns at ss); drc (3 violation(s))`. When `fail-on-electrical: true` is set, the electrical failure keeps the description of a run without the block, `electrical violators in the routed design: ... (fail-on-electrical; see electrical.rpt)`, after the `signoff failed:` prefix. The routed outputs stay. A `--stop-after` or probe run reports the values its stage has and gates nothing.

On a multi-corner platform the [post-route repair rounds](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#repair-timing-after-routing) judge setup only at `setup-corners` and hold only at `hold-corners`. Without the block they judge every corner, and the result has no `signoff` field.
