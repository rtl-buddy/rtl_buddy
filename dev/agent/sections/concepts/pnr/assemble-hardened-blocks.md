## Assemble hardened blocks

A top-level run instances hardened blocks by naming them under `blocks:`, in `pnr.yaml` and in the `synth.yaml` entry the run reads:

```yaml
# pnr.yaml
runs:
  - name: top_pnr
    # ...
    blocks:
      - name: alu_block          # module name as instanced in the top netlist
        pnr: alu_block_pnr       # a harden: true run
        pnr-path: ../alu/pnr.yaml  # default: this pnr.yaml

# synth.yaml
syntheses:
  - name: top_synth
    # ...
    blocks:
      - name: alu_block
        pnr: alu_block_pnr
        pnr-path: ../../pnr/alu/pnr.yaml   # required here
```

P&R takes each block's LEF, Liberty and GDS from its abstract; synthesis takes the Liberty and LEF. 

- **Build the blocks first.** A run never starts a block's run. With no abstract, the consuming run fails before its tool starts, naming the block and the `rb pnr` command that builds it. The top's synthesis also needs the block hardened. `rb pnr` with no run name orders this; see [Run a whole hierarchy](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-a-whole-hierarchy).
- **Same technology and corner.** The block's technology LEF and corner Liberty must match the consuming run's, or the run fails with a platform/corner mismatch. A multi-corner platform cannot consume single-corner abstracts.
- **Stale abstracts are refused.** If an input of the block changed since it was hardened (RTL, netlist, SDC, Liberty, LEF, snippets, its `pnr.yaml` or platform), the consuming run fails, naming the block, what changed and the command that re-hardens it. `--accept-stale` on `rb pnr` or `rb synth` consumes it anyway and the result description says `stale block abstract(s) accepted: <names>`.
- **Blackbox the module.** The top's filelist must leave the block's module a blackbox, typically a port-only stub.
