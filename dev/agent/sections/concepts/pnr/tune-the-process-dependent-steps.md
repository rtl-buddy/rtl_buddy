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

cfg-pnr-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    cts-buffer: [sky130_fd_sc_hd__clkbuf_4, sky130_fd_sc_hd__clkbuf_8]
    placement:
      density: 0.6         # this platform only; padding stays the PDK's 2
```

**Placement.** `density` is the `global_placement` target utilization (`> 0` and `<= 1`); `padding` is the cell padding in sites, applied to both `-pad_left` and `-pad_right`. Both belong on the PDK, where they describe the cell architecture, and a P&R platform may override either of them for one selection — field by field, so a platform that names only `density` keeps the PDK's `padding`. Padding applies to global placement; detailed placement runs unpadded, as it always has.

**Macro halo.** `macro-halo` is the minimum channel, in microns, the [macro packer](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#macro-placement) keeps between two macros and between a macro and each core edge. The default is 20.0 µm, which is what `pdngen` needs to repair a channel rather than fail it: a repair has to fit two straps and the spacing between them inside the halo the PDN's own macro grid reserves, and on sky130hd — met4/met5 straps 1.6 µm wide, a default spacing of half the 27.14 µm pitch less the width, and 2 µm of macro-grid halo per side — that is about 19.2 µm. A 12 µm channel measurably is not enough: it leaves `[ERROR PDN-0179] Unable to repair all channels`. Nangate45 ships no `pdn-config`, so there the halo costs placement area only, about fourteen standard-cell rows. Raise it for a process with a coarser grid, or when a macro needs more routing room; lower it, at the cost of PDN repairability, when macros will not otherwise fit.

**Don't-use cells.** `dont-use-cells` is one list of cell names or `*`-patterns, written on the PDK and read by both flows: P&R emits `set_dont_use` before the floorplan, so no placement, repair or CTS pass can pick one, and synthesis passes the same patterns to Yosys (`dfflibmap -dont_use`, `abc -dont_use`) and, on the OpenROAD backend, to the resynthesis stage's own `set_dont_use`. Write one pattern per list entry; an entry containing whitespace is rejected. Matching is each tool's own: OpenROAD and `dfflibmap` take simple globs, `abc` forwards each pattern to ABC's library exclusion, so confirm the routed netlist is free of the cells you meant to exclude.

**Power grid.** `pdn-config` is a path — resolved from `root_config.yaml`, like the PDK's other paths — to a Tcl snippet that *declares* the grid: `add_global_connection`, `set_voltage_domain`, `define_pdn_grid`, `add_pdn_stripe`, `add_pdn_connect`. The flow sources it after macro placement and calls `pdngen` itself, the same split ORFS uses for `PDN_TCL`, so the snippet must not call `pdngen`. Leave the key unset and no PDN Tcl is emitted at all. A snippet the config names and the disk does not have fails the run at `setup`, before OpenROAD is launched.

**CTS buffers.** `cts-buffer` still takes a single name. Given a list, every entry becomes the CTS `-buf_list` and the first entry becomes `-root_buf`, so order the list with the root buffer first. `cts-buffer: [BUF_X4]` and `cts-buffer: BUF_X4` are the same configuration.

### Macro placement

A design whose netlist instantiates hard macros — an SRAM, or a partition hardened by an earlier P&R run — gets an automatic macro placement before the power grid is built. Every block instance is packed by its *own* footprint: macros are sorted tallest first (then widest, then by name, so the result never depends on the order the database returns instances) and packed left to right into rows whose height is the tallest macro in the row, keeping `placement.macro-halo` between neighbours and at every core edge. Each origin is snapped up to the manufacturing grid — up, so snapping can only widen a channel — and the instance is left `FIRM`, which global placement and the detailed placer then respect.

Rows start at the core's bottom-left corner, so macro locations differ from the grid placement this replaced: a single macro sits in the corner, a halo from both edges, where it used to be centred in the core. Macros still place legally in every case that placed before, and the free area they leave is contiguous instead of split around a centred block.

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
