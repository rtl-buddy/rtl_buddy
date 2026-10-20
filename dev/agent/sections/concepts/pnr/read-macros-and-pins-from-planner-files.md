## Read macros and pins from planner files

A floorplanner that writes hundreds of macro locations or thousands of pin positions can hand them over as files instead of `pnr.yaml` entries:

```yaml
    floorplan:
      macros-file: plan/macros.yaml   # floorplan.macros entries
      macro-snap: false               # exact locations, no site or row snapping
      pins-file: plan/pins.json       # exact pins and floorplan.pins entries
      pin-snap: false                 # exact pin centres, not moved to the edge or a track
      pins-complete: true             # every signal port must be covered
```

```yaml
# plan/macros.yaml
- {instance: u_core/u_sram0, location: [120.0375, 80.64], orientation: R90}
- {instance: "u_core/u_rf*", halo: [2, 2]}
```

```json
[
  {"name": "din[0]", "location": [0.5, 68.34], "layer": "met3", "size": [1.0, 0.3]},
  {"names": ["dout*"], "side": "right"}
]
```

- Each file is a YAML or JSON list, its path relative to `pnr.yaml`. It is read when the run starts, before OpenROAD, so `rb pnr --list` does not need it. A missing file, a malformed entry or an unknown key fails the run with `fail_stage: setup`, naming the file and the entry's index.
- A macros-file entry has the [`floorplan.macros`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#place-individual-macros) keys. A pins-file entry is either an exact pin, `name` (or `names` with one name) with `location`, optional `layer` and optional `size`, or any [`floorplan.pins`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#constrain-boundary-pins) entry. `size` is `[width, height]` in microns, the pin's x and y extents, as in `floorplan.pins` and `place_pin -pin_size`.
- File entries apply after the inline ones and keep their order: exact pins from `floorplan.pins`, then from the file, then the inline and file side and group rules, then `pin-constraints`. Messages name them `floorplan.macros-file[i]` and `floorplan.pins-file[i]`.
- File coordinates reach OpenROAD at full precision and are rounded once, to the database unit. Inline coordinates are rounded to the nanometre.
- Both files are fingerprinted as inputs of a [checkpoint](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints) run and of a [hardened block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block), so editing one makes the block's abstract stale.

`macro-snap: false` is described under [Place individual macros](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#place-individual-macros). `pin-snap: false` (default `true`) places each exact pin, inline or from the file, with its centre at its location to the database unit: `place_pin` runs without `-force_to_die_boundary`, so the pin is neither moved onto the die edge nor onto a routing track. Give a pin on an edge a centre half its depth inside the die. Without the force, placing a pin is also much faster, which matters for thousands of pins. The log still reports each pin's requested and final centre.

### Check that every port is placed

`floorplan.pins-complete: true` (default `false`) checks, just before the `pin-constraints` file and `place_pins`, that every port other than a power or ground port is covered by the inline and file pin entries. An exact pin covers its port; a side or group rule covers the ports it matches that have no exact pin. The run fails, listing up to ten ports per problem and the count:

- a port with more than one exact entry;
- a port with no exact entry that several side or group rules match;
- a port nothing matches.

The message reads `floorplan.pins-complete: 2 port(s) matched by no entry (irq, test_mode)`, and the result carries `fail_stage: pins_complete`, so `xfail:` does not excuse it. A port constrained only by the `pin-constraints` file counts as unmatched.

### Check the routed floorplan

A run with a located macro or an exact pin compares them with the routed `<top>.def` after OpenROAD succeeds. `pnr.log` records each as placed, `RB-FLOORPLAN-MACRO: <x> <y> <orientation> <instance>` and `RB-FLOORPLAN-PIN: <x0> <y0> <x1> <y1> <layer> <port>` in database units, and the check reads only the DEF's COMPONENTS and PINS. A macro at another location or orientation, or a pin with another layer or box, fails the run with `fail_stage: routed_check`, naming the first three differences, so `xfail:` does not excuse it. The routed outputs stay for inspection and no abstract is published.
