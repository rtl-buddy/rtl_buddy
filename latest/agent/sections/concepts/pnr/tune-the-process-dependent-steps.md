## Tune the process-dependent steps

The flow's placement, clock-tree and power-grid steps are calibrated for Nangate45. A different process declares its own values; every key below is optional, and a config that sets none of them gets the behaviour the flow had before they existed.

```yaml
cfg-pdks:
  - name: sky130hd
    # ...
    placement:
      density: 0.55        # default 0.7
      padding: 2           # default 1, in sites
      macro-halo: 30.0     # default 20.0, in microns
    dont-use-cells:
      - "sky130_fd_sc_hd__probe*"
    pdn-config: pdk/sky130hd/pdn.tcl
    rcx-rules: pdk/sky130hd/rcx_patterns.rules

cfg-pnr-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    cts-buffer: [sky130_fd_sc_hd__clkbuf_4, sky130_fd_sc_hd__clkbuf_8]
    placement:
      density: 0.6         # this platform only; padding stays the PDK's 2
    dont-use-cells:        # added to the PDK's list, never replacing it
      - "sky130_fd_sc_hd__lpflow_*"
```

**Placement.** `density` is the `global_placement` target utilization (`> 0` and `<= 1`); `padding` is the cell padding in sites, applied to both `-pad_left` and `-pad_right`. Both belong on the PDK, where they describe the cell architecture, and a P&R platform may override either of them for one selection — field by field, so a platform that names only `density` keeps the PDK's `padding`. Padding applies to global placement; detailed placement runs unpadded, as it always has.

**Macro halo.** `macro-halo` is the minimum channel, in microns, the [macro packer](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#macro-placement) keeps between two macros and between a macro and each core edge. The default is 20.0 µm, which is what `pdngen` needs to repair a channel rather than fail it: a repair has to fit two straps and the spacing between them inside the halo the PDN's own macro grid reserves, and on sky130hd — met4/met5 straps 1.6 µm wide, a default spacing of half the 27.14 µm pitch less the width, and 2 µm of macro-grid halo per side — that is about 19.2 µm. A 12 µm channel measurably is not enough: it leaves `[ERROR PDN-0179] Unable to repair all channels`. Nangate45 ships no `pdn-config`, so there the halo costs placement area only, about fourteen standard-cell rows. Raise it for a process with a coarser grid, or when a macro needs more routing room; lower it, at the cost of PDN repairability, when macros will not otherwise fit.

**Don't-use cells.** `dont-use-cells` is one list of cell names or `*`-patterns, written on the PDK and read by both flows: P&R emits `set_dont_use` before the floorplan, so no placement, repair or CTS pass can pick one, and synthesis passes the same patterns to Yosys (`dfflibmap -dont_use`, `abc -dont_use`) and, on the OpenROAD backend, to the resynthesis stage's own `set_dont_use`. Write one pattern per list entry; an entry containing whitespace is rejected. Matching is each tool's own: OpenROAD and `dfflibmap` take simple globs, `abc` forwards each pattern to ABC's library exclusion, so confirm the routed netlist is free of the cells you meant to exclude.

A synth or P&R platform may name its own `dont-use-cells` for one selection; a platform's list reaches only its own flow, the PDK's reaches both. Patterns support only the `*` and `?` wildcards; Tcl metacharacters (`[ ] { } $ " \ ;`) are rejected when the config loads. They are *added* to the PDK's list — PDK entries first, a pattern named by both kept once — so a platform can exclude more than its PDK but never less, and a platform that adds nothing renders the Tcl the PDK alone did. With any cell excluded, the flow also checks the routed design itself, after hold repair and detailed routing and before fill insertion or any output is written: every placed instance whose master matches a pattern (by the same `get_lib_cells` matching `set_dont_use` used) is printed as `RB-DONT-USE-VIOLATION: <instance> <master> <pattern>` in `pnr.log`, and the run fails naming the first of them. `set_dont_use` only stops the resizer and CTS from *choosing* a cell, so this is what catches one the synthesis netlist already instantiates, and a don't-use cell is never reported as a plain PASS (#656); like any other verdict on the design, an `xfail:` marker on the run can still excuse it. A pattern that matches no Liberty cell excludes nothing — OpenROAD reports `[WARNING STA-0122] cell '<pattern>' not found.` (or `STA-0121` for the library half of a `lib/cell` pattern) and carries on — so rb logs a `pnr.dont_use_unmatched` warning naming it; check it for typos. With no cell excluded, neither the `set_dont_use` nor the check is emitted.

**Power grid.** `pdn-config` is a path — resolved from `root_config.yaml`, like the PDK's other paths — to a Tcl snippet that *declares* the grid: `add_global_connection`, `set_voltage_domain`, `define_pdn_grid`, `add_pdn_stripe`, `add_pdn_connect`. The flow sources it after macro placement and calls `pdngen` itself, the same split ORFS uses for `PDN_TCL`, so the snippet must not call `pdngen`. Leave the key unset and no PDN Tcl is emitted at all. A snippet the config names and the disk does not have fails the run at `setup`, before OpenROAD is launched.

**Parasitic extraction.** `rcx-rules` is a path, resolved like `pdn-config`, to an OpenRCX extraction-rules file (ORFS `RCX_RULES`). With it set, the flow runs extraction after fill insertion and writes the result as `<top>.routed.spef`:

```tcl
define_process_corner -ext_model_index 0 X
extract_parasitics -ext_model_file <rcx-rules>
write_spef $OUT_DIR/${DESIGN}.routed.spef
```

and then reads that SPEF back with `read_spef` in place of `estimate_parasitics -global_routing`, so the final `report_worst_slack` / `report_tns` / `timing.rpt` — and therefore the summary's WNS and TNS — are timed on extracted parasitics, as OpenROAD's own test flow and the ORFS final report do. A [`netlist-source: pnr` power run](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/power/#extracted-parasitics) then reads the same SPEF. Leave the key unset and nothing changes: no extraction, and the reports keep the global-route estimate. A rules file the config names and the disk does not have fails the run at `setup`, before OpenROAD is launched, rather than after detailed route. The rules must match the technology LEF's routing stack (the file's `LayerCount`).

**CTS buffers.** `cts-buffer` still takes a single name. Given a list, every entry becomes the CTS `-buf_list` and the first entry becomes `-root_buf`, so order the list with the root buffer first. `cts-buffer: [BUF_X4]` and `cts-buffer: BUF_X4` are the same configuration.

### Macro placement

A design whose netlist instantiates hard macros — an SRAM, or a partition hardened by an earlier P&R run — gets an automatic macro placement before the power grid is built. Every block instance is packed by its *own* footprint: macros are sorted tallest first (then widest, then by name, so the result never depends on the order the database returns instances) and packed left to right into rows whose height is the tallest macro in the row, keeping `placement.macro-halo` between neighbours and at every core edge. Each origin is snapped up to the standard-cell site grid counted from the core corner (the site width in x, the row height in y) — up, so snapping can only widen a channel — and the instance is left `FIRM`, which global placement and the detailed placer then respect. Row alignment is what keeps the detailed placer's padding check (`DPL-0011`) honest: it rounds a macro to its nearest row, so a bottom edge between two rows can make it scan the row of standard cells above the macro as the macro's own.

Rows start at the core's bottom-left corner (or the corner [`floorplan.macro-anchor`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#floorplan-controls) names), so macro locations differ from the grid placement this replaced: a single macro sits in the corner, a halo from both edges, where it used to be centred in the core. Macros still place legally in every case that placed before, and the free area they leave is contiguous instead of split around a centred block.

Packing by footprint is what lets a mixed-size set fit a floorplan sized for the design. Three macros of 479.78 × 397.50, 98.94 × 98.94 and 89.47 × 89.47 µm need a 695 × 695 µm core this way; sized into equal slots, as the flow did before, the smallest core that held them was 959.56 × 795.00 µm (#626).

When the macros do not fit, the error says what was tried — the halo, the largest footprint, the macro area against the core area — and the smallest core at the run's aspect ratio that this packer would fit, so the `floorplan` block can be corrected in one step:

```
3 macros do not fit the 500.000 x 500.000 um floorplan core
  tried: shelf packing, tallest macro first, keeping a 20.000 um halo (placement.macro-halo) at every core edge and between macros
  macros: largest footprint 479.780 x 397.500 um, 208506.6 um2 of macro area in a 250000.0 um2 core
  smallest core at this aspect ratio that would fit: 556.440 x 556.440 um
  lower the floorplan utilization, change its aspect ratio, or lower placement.macro-halo
```

A design with no macros is unaffected: the packer's procedures are defined in `pnr.tcl` and never called.

### Floorplan controls

Three optional `floorplan` keys steer where macros and standard cells may go (#105). A run that sets none of them renders the flow it always did.

```yaml
    floorplan:
      utilization: 0.45
      core-margin: 10.0
      macro-anchor: upper-right        # default lower-left
      blockages:
        - rect: [10, 10, 130, 60]      # microns, die coordinates: x0 y0 x1 y1
        - rect: [300, 10, 360, 200]
          type: soft
        - rect: [400, 300, 520, 420]
          type: partial
          max-density: 0.4
```

**Macro anchor.** `macro-anchor` is the core corner the [macro packer](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#macro-placement) starts from: `lower-left` (the default, the packing described above), `lower-right`, `upper-left` or `upper-right`. The rows fill away from that corner, so the two edges opposite it stay clear of macros for as long as the macros allow — the edges to point `pin-constraints` at when a neighbouring block abuts one side. An anchored packing is the default packing reflected across the core's centre lines, so the halo, the order and the channels are the same; only the snapping differs, because the site grid still counts from the core's lower-left corner: a mirrored macro's origin is snapped *down*, away from the anchor edge, which again only ever widens a channel. The setting is per run, not per platform, because it is part of the design's pin plan.

**Placement blockages.** Each `blockages` entry is a rectangle, `rect: [x0, y0, x1, y1]` in microns in die coordinates (the die's lower-left corner is the origin), with a `type`:

| `type` | Effect |
| --- | --- |
| `hard` (default) | No standard cell is placed inside. The macro packer also keeps every macro out of it: a macro that would overlap one moves along its row past it, and a row it leaves no room in is skipped. No halo is kept to a blockage. |
| `soft` | Global placement keeps standard cells out; later repair and legalization may still use the area. Macros may sit on it. |
| `partial` | Global placement caps the cell density inside at `max-density`, a fraction strictly between 0 and 1. The cap is **global placement's only**: OpenROAD's detailed placer treats every non-soft blockage as fully blocked, so the legalization passes that follow move the cells out again and the area ends up behaving like a `hard` one for standard cells. Macros may sit on it. |

The flow creates them with OpenROAD's `create_blockage` right after the floorplan, ahead of macro and global placement. A malformed rectangle (`x0 >= x1`, `y0 >= y1`, a side under 0.001 µm, a negative or non-finite coordinate, not four numbers), an unknown type, or a `max-density` on anything but a `partial` blockage fails when `pnr.yaml` loads; a rectangle outside the die fails in OpenROAD. `create_blockage` first shipped in OpenROAD 26Q1, so a run with blockages fails at setup on an older build. When hard blockages leave the macros no room, the no-fit error says how many blockages it avoided and lists moving one among the fixes.
