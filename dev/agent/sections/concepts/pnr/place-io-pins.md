## Place IO pins

`floorplan.pins` constrains IO pins by port name or glob:

```yaml
    floorplan:
      pins:
        - {names: [req_*, clk], side: left}
        - {names: rsp_*, side: right, start: 10, end: 80}   # die µm along the edge
        - {names: pwdata, side: top, group: true, order: true}
        - {names: irq, location: [0, 42.5], layer: met3, size: [0.3, 0.6]}
```

- A `location` entry places one pin exactly, moved onto the nearest die edge and routing track. `pin-snap: false` keeps its centre where given.
- Exact pins are placed first; side and group rules skip them. When every signal port has an exact pin, `place_pins` is skipped.
- A name that matches no port fails the run.
- `pins-file` takes a planner's YAML or JSON list of exact pins and `floorplan.pins` entries.
- `pins-complete: true` fails the run (`fail_stage: pins_complete`) when a signal port has no matching entry, more than one exact pin, or no exact pin and several side or group rules.

For other pin Tcl, such as `-mirrored_pins` or `exclude_io_pin_region`, set `pin-constraints: pins.tcl` on the run. It is sourced just before pin placement. Do not constrain a port that has an exact pin there (`PPL-0107`), and do not put pin commands in the SDC, which is read before the die exists.

After routing, every located macro and exact pin is compared with the routed DEF; a moved one fails the run with `fail_stage: routed_check`.
