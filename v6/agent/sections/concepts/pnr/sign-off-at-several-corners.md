## Sign off at several corners

`corners:` in place of `corner:` analyses a list of the PDK's corners together. `rb pnr` and `rb power` read it; `rb synth` stays single-corner.

```yaml
cfg-pdks:
  - name: sky130hd
    corners:
      tt: pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
      ss: pdk/sky130hd/lib/sky130_fd_sc_hd__ss_n40C_1v40.lib

cfg-pnr-platforms:
  - name: sky130hd_mc
    pdk: sky130hd
    corners: [tt, ss]    # the first entry is the primary corner
```

- All corners run in one OpenROAD session, so repair and the final worst-slack and TNS reports see every corner. Clock-tree synthesis uses the primary (first) corner.
- `corner` and `corners` are mutually exclusive. The list must be non-empty, and each name must be declared in the PDK, appear once, and use only letters, digits, `_`, `.` and `-`. A one-entry list is the same run as `corner:`. A platform that sets neither uses the PDK's first corner.
- A macro with one Liberty in `lib-paths` is read into every corner and keeps the timing of the corner it was characterised at, so a slow corner can fail because of the macro (for example `RSZ-0090` from a TT SRAM Liberty). Give such a macro a Liberty per corner. OpenSTA logs `STA-1140 library ... already exists` once per extra corner; that is expected.
