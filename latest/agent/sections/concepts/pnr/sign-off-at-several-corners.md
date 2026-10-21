## Sign off at several corners

Give a platform `corners: [tt, ss, ff]` instead of `corner:`. All corners are analysed in one OpenROAD session, so repair and the final reports see every corner. Clock-tree synthesis uses the first, the primary. Without either key the PDK's first corner is used. `rb power` reads the list too, in one SDC mode ([Known Issues](https://rtl-buddy.github.io/rtl_buddy/v7/known-issues/#rb-power-times-every-corner-in-one-sdc-mode)); `rb synth` stays single-corner.

Each corner is timed in an OpenSTA SDC mode and scene of its own, named after the corner, so a generated clock's latency comes from the corner being timed:

- The SDC, `max-fanout` and the propagated clocks after CTS apply in every mode. The final reports read the routed SPEF once per corner.
- `constraints:` can give each corner its own SDC, as a map that names every corner. Define the same clocks in each, since CTS builds the clock trees from the primary corner's.

  ```yaml
      constraints: {tt: top_tt.sdc, ss: top_ss.sdc, ff: top_ff.sdc}
  ```

- The primary corner's mode is the one CTS, hooks and an SDC command without a mode use. To constrain every corner from a hook, run the command after `set_mode <corner>` for each corner, then `set_mode <primary>`.
- `<top>.routed.sdc` holds the primary corner's constraints and `<top>.<corner>.routed.sdc` each corner's. A checkpoint's `.sdc` holds every mode; its `<NN>_<stage>.<corner>.sdc` holds one corner's as a plain SDC.
- `max_fanout_violation_count` counts a pin once per corner it violates at. Without modes (below) it counts the pin once.

A mode per corner needs OpenROAD 26Q4 or newer; rb asks OpenROAD at setup. An older one times every corner in one mode and warns `pnr.corner_modes_unsupported`, and a per-corner `constraints:` fails setup. An OpenROAD that gives no answer twice fails setup with `pnr.corner_modes_probe_failed`. See [Known Issues](https://rtl-buddy.github.io/rtl_buddy/v7/known-issues/#corners-share-one-sdc-mode-on-an-openroad-without-modes).

A `lib-paths` entry that is one path is timed at every corner, so a macro keeps that file's timing at every corner and can fail a slow corner (`RSZ-0090`). An entry can instead map each corner to the macro's Liberty for it:

```yaml
    lib-paths:
      - {tt: macros/sram_tt.lib, ss: macros/sram_ss.lib, ff: macros/sram_ff.lib}
```

Each file is timed at its corner only. A map must name every corner of the platform and no other, or setup fails with `pnr.lib_paths_corners`; on a single-corner platform it names that one corner. The Liberty time-unit check reads every file, and `--resume` refuses a checkpoint made with another map. A file listed both as a plain entry and in a map fails setup with `pnr.lib_paths_duplicate`, because OpenSTA times a cell with the last copy it read; drop the plain entry. `rb power` reads a map inherited from the P&R run or in its own `lib-paths`, and accepts a platform with fewer corners than the map; see [Power](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/power/).

A multi-corner platform cannot harden a block or consume hardened blocks.
