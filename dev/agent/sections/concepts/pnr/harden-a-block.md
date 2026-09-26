## Harden a block

A block that a larger design instances as a hard macro needs three views of its routed result: an abstract LEF for placement and routing, a Liberty timing model for synthesis and STA, and its layout for stream-out. Set `harden: true` on the block's run and it publishes them beside its routed outputs:

```yaml
runs:
  - name: alu_block_pnr
    # ...
    platform: sky130hd_tt_block
    harden: true
```

```
artefacts/<run>/abstract/
  <top>.lef                 write_abstract_lef -bloat_occupied_layers
  <top>.lib                 write_timing_model (OpenSTA)
  <top>.gds                 the run's strict stream-out
  abstract.manifest.json    fingerprints of every input and output
```

- **In the same session.** The LEF and the Liberty model are written by the P&R run itself, after `write_db`, so the model is characterised against the Liberty set the block was routed against and the propagated clocks CTS left. `-bloat_occupied_layers` reports every layer the block routes on as blocked over its whole footprint.
- **Strict layout.** `harden` implies `--gds` and forces `gds-mode: strict` whatever the run or `--gds-mode` asked: a hardened block with a hole in its layout is never acceptable. It therefore needs KLayout, like any strict export.
- **All or nothing.** The views are staged in `abstract.partial/` and moved into place only once all three and the manifest exist. If one is missing, the run fails with `fail_stage: abstract` and no abstract directory is left; the routed DEF and ODB stay, because P&R itself succeeded. Every rerun of the block removes the previous abstract first, whether or not it still hardens — the abstract belongs to the result the rerun replaces.
- **One corner.** An abstract carries one corner's timing model, so `harden` on a multi-corner platform is refused before OpenROAD starts.
- **No power.** `write_timing_model` writes timing arcs only. A parent's `rb power` sees a hardened block as drawing 0 W; account for the block with its own `rb power` run.

The manifest (`schema_version: 1`) records the block, the run, the platform, the PDK, the OpenROAD path and version, the `technology` it was built on (technology LEF and corner Liberty), and a `{path, size, sha256}` fingerprint of each input and output. Inputs are the RTL sources of the upstream synthesis filelist, the synthesized netlist, the SDC, every Liberty and LEF file, and the pin-constraint and PDN snippets. `config` holds what the files do not say — the files the run is configured to read (synthesis entry, SDC, `lef-paths`, `lib-paths`, `gds-paths`), the floorplan, placement, routing layers, CTS buffers and don't-use cells — with a digest over them. Paths are project-relative POSIX where they can be, as in the other manifests.

### Block power-grid convention

A parent ties a hardened block into its own grid the way it ties in any macro: its top-level straps cross the block and drop vias onto the block's power pins. That only works if the block leaves those top layers free. RTL Buddy does not enforce the split; give the block its own platform that keeps it:

- **The block owns the lower layers; the top layers belong to the parent.** On sky130hd the block routes and straps on met1–met4 and exposes its power straps as pins on met4, and the parent's met5 straps run over it. With `-bloat_occupied_layers`, a single block strap on met5 blocks met5 across the whole block, and the parent's `pdngen` fails.
- **Use a block PDK entry and platform.** Point a copy of the PDK entry at a block-level `pdn-config` whose straps stop below the parent's layers, and set the block platform's `routing-layers` to the same range.
- **Match third-party macros.** An OpenRAM SRAM exposes its power on met4 and met3; a hardened block that does the same looks identical to it at the top.

The project template's sky130hd hierarchical example uses this split: see `pnr/sky130hd/pdn_block.tcl` and the `sky130hd_tt_block` platform there.
