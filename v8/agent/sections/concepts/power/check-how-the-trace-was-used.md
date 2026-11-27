## Check how the trace was used

OpenROAD can read a pin's value from the trace and still not use it in `report_power`. Its `Annotated N pin activities.` line and `report_activity_annotation` count the pins the trace named, not the activities the analysis used. So after the analysis, a SAIF or VCD run records both:

- `activity_annotation`: the `report_activity_annotation` counts, such as `{"saif": 16, "unannotated": 0}`.
- `activity_origins`: how many pins took their activity from each origin as `report_power` used it. The pins are the top-level ports and every leaf-instance pin except power and internal pins, the set `report_activity_annotation` counts. `saif` or `vcd` is the trace, `propagated` is derived from other pins, `clock` is the SDC clock's rate, `input` is the default input rate, and `unknown` is no activity at all, which counts as zero.
- `activity_unused`: the pins the trace annotated whose activity came from elsewhere, other than the clock pins. It gives the count, the number annotated, the count per origin and the first five pin names.
- `activity_annotated_clock`: the annotated pins that take the SDC clock's rate in place of the trace's.

When `activity_unused` is not zero, the run warns (`power.trace_activity_unused`):

```text
power run "demo_power_saif": 4 of the 16 pins the SAIF annotated do not take that activity in report_power (unknown: 4) — m, um/Q, uo/A, uo/Y. ...
```

The usual cause is a cell with a clock-to-output timing arc and no `ff` or `latch` group in its Liberty, such as a RAM macro characterised with a `memory` group. The activity search does not cross a clock-to-output arc and seeds only the outputs of `ff` and `latch` cells, so the macro's outputs and the pins they drive keep activity 0. See [Quirks & Known Issues](https://rtl-buddy.github.io/rtl_buddy/v8/known-issues/#annotated-activity-on-a-macros-outputs-is-not-used). The verdict does not change.

The fields go in `--machine` output. `activity_annotation.rpt` holds the counts and the shorter of the annotated and unannotated pin listings; when something other than the trace also annotated pins, such as `set_power_activity` in the SDC, it holds the annotated listing, which names each pin's source. `activity_census.rpt` holds the per-origin counts and, for each pin whose activity is from neither the trace nor propagation, its origin, density and duty. On a multi-corner platform the census reads the worst corner, the one the totals report. The census costs one property lookup per pin, a few seconds per million pins. Set `activity.origin-census: false` to skip it and keep only the counts.
