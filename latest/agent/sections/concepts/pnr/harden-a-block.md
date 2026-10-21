## Harden a block

`harden: true` publishes a routed block as a hard macro for a larger design: `abstract/<top>.lef`, `<top>.lib` (timing only), `<top>.gds` and `abstract.manifest.json`, all or nothing (`fail_stage: abstract`). `<top>.lib` lists the supply pins last and is the same whether the run was staged or one session. `<top>.params.json` records the block's parameter values; it is left out, with the warning `pnr.block_params_unrecorded`, when Yosys cannot elaborate the top.

```yaml
runs:
  - name: alu_block_pnr
    # ...
    platform: sky130hd_tt_block
    harden: true
```

- It forces `--gds` with `gds-mode: strict`, so it needs KLayout and the PDK's `klayout-tech`. `abstract: {gds: false}` publishes no GDS instead, records it as `null` in the manifest and leaves stream-out to `--gds`.
- It needs a single-corner platform.
- It turns on `buffer-ports`, so each port drives or is driven by one buffer and the abstract's pin load does not depend on the block's internals. Set the platform's `port-buffer`; on sky130hd OpenROAD otherwise picks a delay cell.
- The abstract LEF blocks every layer the block uses over its whole area. `abstract: {bloat: 0}` writes the real shapes, a number grows them, and `feedthrough-layers: [met4]` writes real shapes on the listed layers only, so the parent can route across. Check the parent's DRCs after changing it.
- A parent's `rb power` sees the block as 0 W. Run `rb power` on the block itself.

**Power grid.** The parent's top straps cross the block and drop vias onto its supply pins, so the block must leave the top layer free. Give the block its own PDK copy with a `pdn-config` whose straps stop below the parent's (on sky130hd: block met1 to met4 with power pins on met4, parent straps on met5), and set the block platform's `routing-layers` to match. The template's `pdn_block.tcl` and `sky130hd_tt_block` show this. rb ties each block supply pin to the parent net of the same name, or the only power or ground net.

A block that must use the top layer, such as one with an OpenRAM sky130 SRAM, exposes its supply pins there. rb then joins each pin to the parent strap of its net on the same track. For that to work, build the block's top straps on the parent's width, pitch and net order, place it a whole number of pitches from the parent's strap offset, and define the parent's macro grids with `-cells` or `-instances` instead of `-default`. A pin it cannot join fails with `block power: <inst>/<pin> ...`.
