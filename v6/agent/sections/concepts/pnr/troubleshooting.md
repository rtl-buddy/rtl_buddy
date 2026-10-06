## Troubleshooting

- **`pdn-config not found` or a missing `rcx-rules` file.** Fix the path in the PDK entry. The run stops at `setup`.
- **Blockages rejected at setup, or an older-OpenROAD warning.** Upgrade OpenROAD (25Q1 minimum, 26Q1 for blockages).
- **`[ERROR PDN-0179] Unable to repair all channels`.** Raise `placement.macro-halo`.
- **`N macros do not fit ...`.** Lower utilization, change the aspect ratio or lower `macro-halo`.
- **`RSZ-0090` at a slow corner.** A single-Liberty macro limits that corner. Give it a Liberty per corner.
- **`RB-DONT-USE-VIOLATION`.** The netlist instantiates an excluded cell. Change the RTL, synthesis or pattern.
- **`STA-0122 cell '<pattern>' not found`.** A `dont-use-cells` pattern matches nothing.
- **`openroad.threads_capped`.** Raise the reservation, lower `threads:` or use `auto`.
- **`GDS incomplete` or `pnr.gds_incomplete`.** Add the cells' GDS to `gds-paths`, or list intentional gaps in `gds-allow-empty`.
- **`fail_stage: export`.** KLayout, `klayout-tech`, an input file or a render failed. Fix it and run `rb pnr-export`.
- **`fail_stage: abstract`, or `harden:` refused.** A view could not be produced (read `pnr.log`), or the platform has several corners.
- **A missing or stale abstract, or a platform/corner mismatch.** Run the `rb pnr` command the message names, run `rb pnr` with no run name, or pass `--accept-stale`.
- **`fail_stage: blocked`.** A block the run consumes failed. Fix it first.
- **`block power: ... is not on a parent <net> strap`, `no single parent power net`, or `PDN-0233` on a block's macro grid.** A block's supply pins are off the parent's straps or cannot be tied. See [Blocks with supply pins on the top strap layer](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#blocks-with-supply-pins-on-the-top-strap-layer).
- **`instance '<inst>' ... of block '<blk>' sets <P>=...`, `port '<p>' is connected to N bit(s)`, `instances of block '<blk>' have different parameters`, or `... named by a hash`.** The parent instances a block with parameters or ports it was not hardened with. See [Instance a parameterised block](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#instance-a-parameterised-block).
- **`fail_stage: error`.** The run crashed; the exception is in the row description. Its consumers are blocked and other runs still report.
