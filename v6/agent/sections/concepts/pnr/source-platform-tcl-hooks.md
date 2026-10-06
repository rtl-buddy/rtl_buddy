## Source platform Tcl hooks

Four optional PDK paths, resolved like `pdn-config`, name Tcl scripts the flow sources at the step where OpenROAD-flow-scripts (ORFS) sources its own. A platform taken from ORFS, such as ASAP7, needs them.

| Key | ORFS variable | Sourced |
| --- | --- | --- |
| `platform-tcl` | `PLATFORM_TCL` | Before the first `read_liberty`, so `suppress_message` lines cover the Liberty reads |
| `layer-rc-tcl` | `SET_RC_TCL` | After `read_sdc`, so `repair_design`, CTS, hold repair and the final reports see wire RC |
| `tracks-tcl` | `MAKE_TRACKS` | After `initialize_floorplan`, instead of the bare `make_tracks` |
| `tapcell-tcl` | `TAPCELL_TCL` | After macro placement and the macro keep-outs, before the power grid |

```yaml
cfg-pdks:
  - name: asap7
    # ...
    platform-tcl: pdk/asap7/liberty_suppressions.tcl
    layer-rc-tcl: pdk/asap7/setRC.tcl
    tracks-tcl: pdk/asap7/make_tracks.tcl
    tapcell-tcl: pdk/asap7/tapcell.tcl
```

- An unset key leaves the flow as it is: `make_tracks` with the technology LEF's default tracks, no taps, and the LEF's layer RC, which is zero on ASAP7.
- Each file is sourced as written, so it must not depend on ORFS environment variables. ORFS' ASAP7 `openRoad/tapcell.tcl` takes both its tap and endcap master from `$::env(TAP_CELL_NAME)` and its macro halo from `$::env(MACRO_ROWS_HALO_X)` and `$::env(MACRO_ROWS_HALO_Y)`; copy it with those values filled in.
- A configured file missing from disk fails the run at `setup`, naming the key.
- `rb power` sources `platform-tcl` before its Liberty reads. The layer RC is session state that the routed ODB does not keep, so a [`netlist-source: pnr` power run](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/power/#extracted-parasitics) also sources `layer-rc-tcl` after `read_sdc`.
- The hooks are inputs to stage checkpoints and to a [hardened block's](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#harden-a-block) abstract, so changing one makes the abstract stale.
