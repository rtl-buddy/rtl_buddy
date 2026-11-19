## Harden a block

`harden: true` publishes a routed block as a hard macro for a larger design: `abstract/<top>.lef`, `<top>.lib` (timing only), `<top>.gds` and `abstract.manifest.json`, all or nothing (`fail_stage: abstract`).

```yaml
runs:
  - name: alu_block_pnr
    # ...
    platform: sky130hd_tt_block
    harden: true
```

- `<top>.lib` lists supply pins last, the same for staged and one-session runs. `<top>.params.json` records the parameter values; when Yosys cannot elaborate the top it is left out with `pnr.block_params_unrecorded`.
- It forces `--gds` with `gds-mode: strict`, so it needs KLayout and `klayout-tech`. `abstract: {gds: false}` publishes no GDS, records `null` in the manifest and leaves stream-out to `--gds`.
- It needs a single-corner platform.
- It turns on `buffer-ports`, so each port has one buffer and the abstract's pin load does not depend on the internals. Set the platform's `port-buffer`; on sky130hd OpenROAD otherwise picks a delay cell.
- The abstract LEF blocks every layer the block uses over its whole area. `abstract: {bloat: 0}` writes real shapes and a number grows them; `feedthrough-layers: [met4]` writes real shapes on those layers only, so the parent can route across. Check the parent's DRCs after changing it.
- A parent's `rb power` sees the block as 0 W; run `rb power` on the block.

The parent's top straps cross the block and drop vias onto its supply pins, so the block must leave the top layer free:

- Give the block its own PDK copy whose `pdn-config` straps stop below the parent's. On sky130hd the block uses met1 to met4 with pins on met4, and the parent's straps are on met5.
- Match the block platform's `routing-layers`. The template's `pdn_block.tcl` and `sky130hd_tt_block` show this.
- rb ties each block supply pin to the parent net of the same name, or to the only power or ground net.

A block that must use the top layer, such as one with an OpenRAM sky130 SRAM, exposes its supply pins there, and rb joins each to the parent strap of its net on the same track. For that:

- Build the block's top straps on the parent's width, pitch and net order.
- Place the block a whole number of pitches from the parent's strap offset.
- Define the parent's macro grids with `-cells` or `-instances` instead of `-default`.
- A pin rb cannot join fails the run with `block power: <inst>/<pin> ...`.
