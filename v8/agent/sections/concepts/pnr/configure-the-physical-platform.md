## Configure the physical platform

A run picks its technology with `platform:`. A `cfg-pdks` entry supplies files and cell names; a `cfg-pnr-platforms` entry selects a PDK, its corners and per-platform settings. P&R reads these PDK keys beyond synthesis's:

| PDK key | Use |
| --- | --- |
| `tie-hi`, `tie-lo`, `fill-cells`, `pin-layers` | Tie cells, fillers, horizontal and vertical pin layers. `pin-layers` is required: `place_pins` has no default. |
| `cell-gds`, `klayout-tech`, `klayout-props` | Cell layouts, KLayout technology and colours, for stream-out. |
| `pdn-config` | Tcl declaring the power grid, without a `pdngen` call. Unset means no grid. |
| `rcx-rules` | OpenRCX rules, enabling extraction, `<top>.routed.spef` and post-route repair. `LayerCount` must match the LEF. |
| `layer-rc-tcl` | `set_layer_rc` and `set_wire_rc` lines, sourced after `read_sdc`. See [Set wire RC](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#set-wire-rc). |
| `platform-tcl` | Sourced before the first Liberty read, for lines such as `suppress_message`. |
| `tracks-tcl` | Replaces the bare `make_tracks` after floorplan initialisation. |
| `tapcell-tcl` | Inserts taps and endcaps after macro placement, before the grid. Unset runs `cut_rows`, cutting rows 2 µm around each macro. |
| `dont-use-cells` | Cells no step may choose; synthesis reads it too. |
| `placement.*` | Placement defaults; platforms and runs override them. |

The four Tcl keys match ORFS's `PLATFORM_TCL`, `SET_RC_TCL`, `MAKE_TRACKS` and `TAPCELL_TCL`. They are sourced as written, so a file copied from ORFS must not read ORFS environment variables. A missing file fails setup.

`dont-use-cells` takes one name or glob per entry, with `*` and `?` only. A platform's list adds to its PDK's, for its own flow only. After routing, each instance of an excluded cell prints `RB-DONT-USE-VIOLATION: <instance> <master> <pattern>` and fails the run.
