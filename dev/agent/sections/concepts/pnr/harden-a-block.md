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
- An abstract carries one corner's timing model, so `harden` on a multi-corner platform is refused before OpenROAD starts.
- The model has timing arcs only, so a parent's `rb power` sees the block as drawing 0 W. Run `rb power` on the block itself.
