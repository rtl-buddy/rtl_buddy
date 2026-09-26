## Configure the physical platform

PDK files are defined once under `cfg-pdks`. Select a process and corner for P&R under `cfg-pnr-platforms`:

```yaml
cfg-pnr-platforms:
  - name: nangate45_typ
    pdk: nangate45
    corner: typ
    cts-buffer: BUF_X4
    routing-layers:
      signal: metal2-metal8
      clock: metal4-metal8
```

The PDK entry supplies Liberty, technology and macro LEF, cell GDS, site, and other cell names. See [Synthesis: Configure tools and the PDK](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/#configure-tools-and-the-pdk) and the [root config schema](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#root_configyaml).

### Sign off at several corners

`corners:` in place of `corner:` analyses a list of the PDK's corners together. `rb pnr` and `rb power` both read it, since both select their Liberty through the same `cfg-pnr-platforms` entry:

```yaml
cfg-pdks:
  - name: sky130hd
    corners:
      tt: pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
      ss: pdk/sky130hd/lib/sky130_fd_sc_hd__ss_n40C_1v40.lib
      ff: pdk/sky130hd/lib/sky130_fd_sc_hd__ff_n40C_1v95.lib

cfg-pnr-platforms:
  - name: sky130hd_mc
    pdk: sky130hd
    corners: [tt, ss, ff]    # the first entry is the primary corner
```

Every corner is analysed in **one** OpenROAD session, not one session per corner. The flow calls `define_corners` and then reads each corner's Liberty with `read_liberty -corner`. As a result, `repair_design`, `repair_timing` and the hold repair see all corners, and the final `report_worst_slack` and `report_tns` report the worst across them. Clock-tree synthesis is the exception: OpenROAD characterises CTS buffers and wires at its command corner, which is the first corner listed. That is why the first entry of `corners:` is called the primary. A `lib-paths` macro that has a single Liberty is read into every corner, as ORFS does. OpenSTA then logs `STA-1140 library ... already exists` once for each extra corner; the warning is expected. The macro keeps the timing of the corner its Liberty was characterised at, which can make a corner fail for reasons that belong to the macro. For example, a TT SRAM Liberty with `max_transition: 0.04` stops `repair_design` at `RSZ-0090` at a slow corner. Give such a macro per-corner Liberty.

Rules:

- `corner` and `corners` are mutually exclusive.
- An empty list is an error.
- Each name must be declared in the PDK's `corners:` and appear only once.
- Names may contain only letters, digits, `_`, `.` and `-`, because each one becomes an OpenSTA corner name and part of a report file name.
- A one-entry list is the same run as `corner:` with that name.
- A platform that sets neither key keeps the PDK's first corner.
- The generated `pnr.tcl` and `power.tcl` of a single-corner platform are unchanged.

`rb synth` stays single-corner and reads its own `cfg-synth-platforms` entry.
