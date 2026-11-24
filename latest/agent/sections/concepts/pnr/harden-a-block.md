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

### Forward a clock through a hardened block

A block that passes its clock on to an output port, for blocks abutted in a chain, declares it in its SDC as a generated clock of ratio 1:

```tcl
create_generated_clock -name clk_fwd -source [get_ports clk_i] -divide_by 1 [get_ports clk_o]
```

OpenSTA's `write_timing_model` writes `clk_o`'s arcs in `<top>.lib` as clock-to-output arcs, related to `clk_o` itself with that clock and to `clk_i` without it. Timing does not carry a clock through either, so in the parent the clock stops at the first block's `clk_i` and every block after it is unclocked ([Known Issues](https://rtl-buddy.github.io/rtl_buddy/v8/known-issues/#a-hardened-blocks-timing-model-stops-a-forwarded-clock)).

`abstract: {forwarded-clocks: arc}` makes `clk_o` a clock path:

```yaml
    harden: true
    abstract: {forwarded-clocks: arc}
```

- The harden step writes the timing model a second time without the generated clocks defined on output ports. For each generated clock from one input port to one output port with `-divide_by 1` or `-multiply_by 1`, or `-combinational` with neither, optionally `-invert`, the output's timing groups become one `combinational` arc from the input, `positive_unate` (`negative_unate` with `-invert`): the rise from the second model's `rising_edge` arc, the fall from its `falling_edge` arc, each by output load. It is the forwarding delay from the input pin, the block's wire and buffers.
- Every other group of `<top>.lib` is kept as written, including the setup, hold and clock-to-output arcs with the block's insertion delay and the input's `min_clock_tree_path` and `max_clock_tree_path`.
- `abstract.manifest.json` lists the arcs under `forwarded_clocks`, each `{from, to, sense}`. A generated clock on an output port that does not qualify warns `pnr.abstract_forwarded_clock_skipped`, and its output keeps all its arcs as written, so a qualifying clock beside it on that output, such as a bypass beside a divided clock on a clock mux, warns too and is not forwarded. A block with none warns `pnr.abstract_no_forwarded_clocks`. The SDC is read as Tcl words, so an option named inside a `-comment {...}` is not an option. A forwarding arc that falls with the input when the SDC says it does not (a wrong `-invert`) fails the abstract.
- Inside the block, `dont-touch` and `cts-root-taps` keep the forwarding buffer as placed and on the clock's root net, so the arc is that buffer and its wire ([Forward a clock with a root tap](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#forward-a-clock-with-a-root-tap)).
- The parent needs no SDC for the forwarded clock; a `create_generated_clock` on `u_blk/clk_o` still names it. Sign off with flat timing of the stitched netlist and parasitics: the abstract times the boundary on scalar setup and hold values.

Without the key, restate the forwarded clock on each instance's `clk_o` in the parent's SDC, with the block's forwarding delay as source latency.
