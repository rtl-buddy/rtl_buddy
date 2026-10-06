## Block power-grid convention

A parent ties a hardened block into its grid the way it ties in any macro: its top-level straps cross the block and drop vias onto the block's power pins. This works only if the block leaves the top layers free. rtl_buddy does not enforce the split, so give the block its own platform and PDK entry:

- The block owns the lower layers and the parent the top ones. On sky130hd the block routes and straps on met1 to met4 and exposes power pins on met4, and the parent's met5 straps run over it. A block strap on met5 blocks met5 over the whole block, so the parent's straps cannot cross it. A block that needs met5 is handled by [joining its pins](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#blocks-with-supply-pins-on-the-top-strap-layer).
- Point a copy of the PDK entry at a block-level `pdn-config` whose straps stop below the parent's layers, and set the block platform's `routing-layers` to the same range.

The template's `pnr/sky130hd/pdn_block.tcl` and `sky130hd_tt_block` platform show this split.

`rb pnr` ties each block instance's power and ground pins, as the abstract LEF declares them (`USE POWER` / `USE GROUND`), when the parent's `pdn-config` leaves them on no net. A pin goes to the parent supply net of the same name, else to the parent's only power or ground net; with several candidates the run fails and asks for an `add_global_connection`. The log shows `rb: tied <inst>/<pin> to <net>`.
