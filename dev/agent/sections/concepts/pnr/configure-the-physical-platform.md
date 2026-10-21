## Configure the physical platform

A PDK entry in `cfg-pdks` supplies the files and cell names; a `cfg-pnr-platforms` entry selects a PDK, its corner or corners, and the per-platform knobs. A run picks a platform with `platform:`. P&R reads these PDK keys beyond the ones synthesis uses:

| PDK key | Use |
| --- | --- |
| `tie-hi`, `tie-lo`, `fill-cells`, `pin-layers` | Tie cells, filler masters, horizontal and vertical pin layers |
| `cell-gds`, `klayout-tech`, `klayout-props` | Stream-out layout and KLayout technology and colours |
| `pdn-config` | Tcl that declares the power grid (no `pdngen` call). Unset means no grid. |
| `rcx-rules` | OpenRCX rules. Enables extraction, `<top>.routed.spef` and post-route repair. Its `LayerCount` must match the LEF. |
| `layer-rc-tcl` | `set_layer_rc` / `set_wire_rc` lines. Sourced after `read_sdc`. See [Set wire RC](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#set-wire-rc). |
| `platform-tcl` | Sourced before the first Liberty read, such as `suppress_message` lines |
| `tracks-tcl` | Replaces the bare `make_tracks` after floorplan initialisation |
| `tapcell-tcl` | Inserts tap and endcap cells, after macro placement and before the power grid |
| `dont-use-cells` | Cells no step may choose. Also read by synthesis. |
| `placement.*` | Placement defaults, overridable per platform and per run |

The four Tcl keys match ORFS's `PLATFORM_TCL`, `SET_RC_TCL`, `MAKE_TRACKS` and `TAPCELL_TCL`. They are sourced as written, so a file copied from ORFS must not read ORFS environment variables. A missing file fails the run at setup.

`dont-use-cells` takes one name or glob per entry, with `*` and `?` only. A platform's list adds to its PDK's and applies to its own flow only. After routing, any instance of an excluded cell is printed as `RB-DONT-USE-VIOLATION: <instance> <master> <pattern>` and fails the run.
