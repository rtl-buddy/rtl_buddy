## Sign off at several corners

Give a platform `corners: [tt, ss, ff]` instead of `corner:` to time and repair every corner in one OpenROAD session, so repair and the final reports see them all.

- CTS uses the first corner, the primary. With neither key, the PDK's first corner is used.
- `rb power` reads the list too, in one SDC mode ([Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#rb-power-times-every-corner-in-one-sdc-mode)). `rb synth` stays single-corner.
- A multi-corner platform cannot harden a block or consume hardened blocks.

Each corner is timed in an OpenSTA SDC mode and scene of its own, named after it, so a generated clock's latency comes from the corner being timed.

- The SDC, `max-fanout` and the propagated clocks after CTS apply in every mode. The final reports read the routed SPEF once per corner.
- `constraints:` can map every corner to its own SDC: `{tt: top_tt.sdc, ss: top_ss.sdc, ff: top_ff.sdc}`. Define the same clocks in each, since CTS builds the trees from the primary's.
- CTS, hooks and an SDC command without a mode use the primary's mode. To constrain every corner from a hook, run the command after `set_mode <corner>` for each, then `set_mode <primary>`.
- `<top>.routed.sdc` holds the primary's constraints, `<top>.<corner>.routed.sdc` each corner's. A checkpoint's `.sdc` holds every mode, and `<NN>_<stage>.<corner>.sdc` one corner's as plain SDC.
- `max_fanout_violation_count` counts a pin once per corner it violates at; without modes, once.

Modes need OpenROAD 26Q4 or newer, which rb checks at setup ([Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#corners-share-one-sdc-mode-on-an-openroad-without-modes)). An older one times every corner in one mode and warns `pnr.corner_modes_unsupported`, and a per-corner `constraints:` fails setup. An OpenROAD that gives no answer twice fails setup with `pnr.corner_modes_probe_failed`.

A `lib-paths` entry that is one path is timed at every corner, so a macro keeps that timing everywhere and can fail a slow corner (`RSZ-0090`). Map each corner to the macro's Liberty for it instead:

```yaml
    lib-paths:
      - {tt: macros/sram_tt.lib, ss: macros/sram_ss.lib, ff: macros/sram_ff.lib}
```

- Each file is timed at its corner only.
- A map names every platform corner and no other, or setup fails with `pnr.lib_paths_corners`. On a single-corner platform it names that one.
- A file both plain and in a map fails setup with `pnr.lib_paths_duplicate`, because OpenSTA times a cell with the last copy it read. Drop the plain entry.
- The Liberty time-unit check reads every file. `--resume` refuses a checkpoint made with another map.
- `rb power` reads a map from the P&R run or its own `lib-paths`, and accepts a platform with fewer corners than the map; see [Power](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/).
