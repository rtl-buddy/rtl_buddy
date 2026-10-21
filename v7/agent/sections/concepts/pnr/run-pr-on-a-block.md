## Run P&R on a block

You need OpenROAD 25Q1 or newer on `PATH` (or named in `cfg-pnr-tools`) and a synthesis run that has written its netlist. KLayout is needed only for GDS and PNG output. An older OpenROAD logs a warning and runs unvalidated.

Add a P&R platform to `root_config.yaml`. It picks a PDK from `cfg-pdks` (see [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/synthesis/#configure-tools-and-the-pdk)), a corner and the clock-tree buffer:

```yaml
cfg-pnr-platforms:
  - name: nangate45_typ
    pdk: nangate45
    corner: typ
    cts-buffer: BUF_X4
    routing-layers: {signal: metal2-metal8, clock: metal4-metal8}
```

Describe the run in a `pnr.yaml`. Paths are relative to that file. The top module comes from the synthesis entry.

```yaml
rtl-buddy-filetype: pnr_config
runs:
  - name: alu_pnr
    desc: ALU at the typical corner
    synth: alu_synth
    synth-path: ../../synth/alu/synth.yaml
    constraints: ../../synth/alu/alu.sdc
    platform: nangate45_typ
    floorplan: {utilization: 0.55, aspect: 1.0, core-margin: 2.0}
```

```bash
rb pnr --list -c pnr/alu/pnr.yaml
rb pnr alu_pnr -c pnr/alu/pnr.yaml
rb pnr alu_pnr -c pnr/alu/pnr.yaml --png    # also stream out GDS and render a PNG
```

Outputs go to `artefacts/<run>/` next to `pnr.yaml`; each run deletes the previous run's outputs first, except a refused `--resume`, which stops before that and leaves them in place. With no run name, `rb pnr` runs every entry whose `reglvl` is within `-l`, hardened blocks before the runs that use them.
