## Gate signoff

A `signoff:` block on the platform or run makes the verdict fail a routed run on timing, DRCs or electrical violators, at the corners you name:

```yaml
    signoff:
      setup-corners: [ss]                  # default: every corner
      hold-corners: [ff]                   # default: every corner
      fail-on: [timing, drc, electrical]   # default: none
```

| Gate | Fails when |
| --- | --- |
| `timing` | Setup WNS over `setup-corners` or hold WNS over `hold-corners` is negative or not reported. Reports print two digits: `-0.00` fails, `0.00` passes. Setup with no constrained path at every one of its corners fails, since nothing was checked; hold then passes. |
| `drc` | `drc_count` is above 0, or detailed route wrote no DRC report (`drc.count` null). A clean route writes an empty report. |
| `electrical` | Any max-slew, max-capacitance or max-fanout violator, or one of the three counts is not reported. `fail-on-electrical: true` adds this gate, with or without the block. |

- A run's block overrides the platform's field by field; `null` sets a key to its default or turns the block off.
- A corner the platform does not analyse fails the platform's load, or the run's setup.
- A `--stop-after` or probe run reports its stage's values and gates nothing.
- The `signoff` field gives each gate a `verdict` (`pass`, `fail`, `not_gated`) and its values: `timing.setup` and `timing.hold` with `corners`, `wns_ps`, `worst_corner` and `unconstrained`; `drc.count`; the three violator counts. `wns_setup_ps` and `wns_hold_ps` stay the worst over every corner.
- A failed gate fails the run without `fail_stage`, so `xfail:` can excuse it, and the routed outputs stay. The description names each gate, such as `signoff failed: timing (setup WNS -0.1200 ns at ss); drc (3 violation(s))`.
- With `fail-on-electrical: true`, the electrical part keeps its own description, `electrical violators in the routed design: ... (fail-on-electrical; see electrical.rpt)`, after the `signoff failed:` prefix.

On a multi-corner platform, [post-route repair rounds](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-timing-after-routing) judge setup only at `setup-corners` and hold only at `hold-corners`. Without the block they judge every corner, and the result has no `signoff` field.
