## Tune the process-dependent steps

The flow's placement, clock-tree and power-grid steps are calibrated for Nangate45. A different process declares its own values; every key below is optional, and a config that sets none of them generates the same `pnr.tcl` it generated before they existed.

```yaml
cfg-pdks:
  - name: sky130hd
    # ...
    placement:
      density: 0.55        # default 0.7
      padding: 2           # default 1, in sites
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

**Don't-use cells.** `dont-use-cells` is one list of cell names or `*`-patterns, written on the PDK and read by both flows: P&R emits `set_dont_use` before the floorplan, so no placement, repair or CTS pass can pick one, and synthesis passes the same patterns to Yosys (`dfflibmap -dont_use`, `abc -dont_use`) and, on the OpenROAD backend, to the resynthesis stage's own `set_dont_use`. Write one pattern per list entry; an entry containing whitespace is rejected. Matching is each tool's own: OpenROAD and `dfflibmap` take simple globs, `abc` forwards each pattern to ABC's library exclusion, so confirm the routed netlist is free of the cells you meant to exclude.

**Power grid.** `pdn-config` is a path — resolved from `root_config.yaml`, like the PDK's other paths — to a Tcl snippet that *declares* the grid: `add_global_connection`, `set_voltage_domain`, `define_pdn_grid`, `add_pdn_stripe`, `add_pdn_connect`. The flow sources it after macro placement and calls `pdngen` itself, the same split ORFS uses for `PDN_TCL`, so the snippet must not call `pdngen`. Leave the key unset and no PDN Tcl is emitted at all. A snippet the config names and the disk does not have fails the run at `setup`, before OpenROAD is launched.

**CTS buffers.** `cts-buffer` still takes a single name. Given a list, every entry becomes the CTS `-buf_list` and the first entry becomes `-root_buf`, so order the list with the root buffer first. `cts-buffer: [BUF_X4]` and `cts-buffer: BUF_X4` are the same configuration.
