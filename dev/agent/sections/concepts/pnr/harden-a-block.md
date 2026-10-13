## Harden a block

A block that a larger design instances as a hard macro needs three views of its routed result: an abstract LEF, a Liberty timing model and its layout. Set `harden: true` on the block's run to publish them to `artefacts/<run>/abstract/` as `<top>.lef`, `<top>.lib`, `<top>.gds` and `abstract.manifest.json`:

```yaml
runs:
  - name: alu_block_pnr
    # ...
    platform: sky130hd_tt_block
    harden: true
```

- `harden` implies `--gds` and forces `gds-mode: strict`, so it needs KLayout.
- The run also elaborates the block's top in Yosys, with the frontend, sources, defines and Liberty of the block's synthesis, and records the values of its parameters and localparams as `<top>.params.json`. Only names the top declares in its own scope are recorded: a generate loop's genvar, or a localparam inside a generate block, function or procedural block, is not. Parents' overrides are checked against it (see [Instance a parameterised block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#instance-a-parameterised-block)). The probe's script, log and output stay in `param_probe/` of the artefact directory until the next run. If Yosys cannot elaborate the top, the abstract is published without the record and the run warns `pnr.block_params_unrecorded`.
- The views are published together or not at all. If one cannot be produced, the run fails with `fail_stage: abstract` and leaves no `abstract/`; the routed DEF and ODB stay. Each rerun removes the previous abstract first.
- `harden` turns on `buffer-ports`: `buffer_ports -inputs -outputs` runs after IO pin placement and before global placement, as ORFS `global_place.tcl` does, and `repair_design` sizes the buffers. Each input then drives one buffer instead of its internal loads, and each output is driven by one, so the abstract's pin capacitance and drive do not depend on the block's internals. Without it, an input fanning out to a thousand loads carries their whole capacitance into the Liberty model for the parent to drive. OpenROAD leaves clock, constant and special nets unbuffered. The platform's `port-buffer` names the buffer cell; unset, OpenROAD picks one, and on sky130hd it picks the delay cell `sky130_fd_sc_hd__clkdlybuf4s50_1`, so set `port-buffer` there (ORFS uses `sky130_fd_sc_hd__buf_4` as its minimum buffer). Set `buffer-ports: false` to turn it off, or `buffer-ports: true` on a run that does not harden.
- `buffer-ports` is part of the block's configuration only when on, and `port-buffer` only when it is set and ports are buffered, so an abstract hardened by an rtl_buddy without it is stale (`config changed (buffer_ports)`) until the block is re-hardened, and one hardened with `buffer-ports: false` is not.
- An abstract carries one corner's timing model, so `harden` on a multi-corner platform is refused before OpenROAD starts.
- The model has timing arcs only, so a parent's `rb power` sees the block as drawing 0 W. Run `rb power` on the block itself.

### Choose the abstract's obstruction style

By default the abstract LEF is written with `write_abstract_lef -bloat_occupied_layers`: every layer the block uses is an obstruction over its whole footprint. A block with supply straps on met4 then blocks met4 everywhere, and a parent can cross it only on met5. `abstract:` on the hardened run chooses otherwise:

```yaml
runs:
  - name: alu_block_pnr
    harden: true
    abstract:
      bloat: occupied            # default; or a number, OpenROAD's -bloat_factor
      feedthrough-layers: [met4] # these layers get the block's real shapes
```

- `bloat: <number>` writes `-bloat_factor <number>` instead: the block's shapes grown by that factor, so a parent can route between them on every layer. `0` writes the real shapes.
- `feedthrough-layers` keeps the `bloat` style on every other layer and writes the listed ones with the block's real shapes as obstructions. rtl_buddy writes a second LEF with `-bloat_factor 0` and splices its obstructions on those layers into `<top>.lef`. A listed layer the block does not use has no obstruction; one that appears in neither LEF, likely a misspelling, warns `pnr.abstract_feedthrough_layers_unused`.
- `abstract:` needs `harden: true`.
- It is part of the block's configuration only when it differs from the default, so abstracts hardened before it existed stay current, and changing it makes the abstract stale (`config changed (abstract)`) until the block is re-hardened. A hand-edited LEF is still stale.
- With fewer obstructions a parent's router can route over the block. Check the parent's DRC and the block's pins and straps after changing the style.
