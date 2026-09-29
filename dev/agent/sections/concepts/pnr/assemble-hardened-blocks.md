## Assemble hardened blocks

A top-level run instances hardened blocks by naming them under `blocks:`, in `pnr.yaml` and in the `synth.yaml` entry the run reads:

```yaml
# pnr.yaml
runs:
  - name: top_pnr
    synth: top_synth
    synth-path: ../../synth/top/synth.yaml
    platform: sky130hd_tt
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

Each entry resolves to the `abstract/` its `harden: true` run published. P&R appends the abstract's `.lef`, `.lib` and `.gds` to the run's `lef-paths`, `lib-paths` and `gds-paths`, so the flow script, the stream-out manifest and the result fingerprints take the block exactly as they take a hand-wired macro. Synthesis appends the `.lib` and `.lef` to its own lists. The result's `blocks` field lists each consumed block with its run, abstract directory, manifest, the `{path, size, sha256}` fingerprints of the three views it read, and whether it was stale.

- **Build the blocks first.** A named run never starts a block's run. With no abstract — never run, or its last run failed — the consuming run fails before its tool starts, naming the block and the `rb pnr` command that builds it. Synthesis of the top reads the block's Liberty model, so it too runs after the block is hardened. `rb pnr` with no run name does the ordering for you; see [Run a whole hierarchy](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-a-whole-hierarchy).
- **Same technology and corner.** A block may be hardened on its own block-level platform (see [Block power-grid convention](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#block-power-grid-convention)), but its technology LEF and corner Liberty must be the ones the consuming run uses, compared by content. Otherwise the run fails with a platform/corner mismatch. A multi-corner P&R platform cannot consume single-corner abstracts.
- **Stale abstracts are refused.** Before the tool starts, every input the block's manifest recorded — its RTL sources, netlist, SDC, Liberty and LEF files, pin and PDN snippets — and the three published views are fingerprinted again and compared by content, and the block's configuration is rebuilt from its `pnr.yaml` and platform and compared by digest. Any difference fails the consuming run, naming the block, what changed, and the `rb pnr` command that re-hardens it. Timestamps are never used: a checkout or a copy rewrites them in any order. `--accept-stale` on `rb pnr` or `rb synth` consumes a stale abstract anyway; the result description then says `stale block abstract(s) accepted: <names>`, and that block's row in the result has `stale: true` and the list of changes.
- **Blackbox the module in the netlist.** `blocks:` supplies the block's views; it does not change what the model's filelist compiles. The top's filelist has to leave the block's module a blackbox, typically a port-only stub, as it would for any hard macro.
- **Power.** The abstract Liberty has no power data, so a parent's `rb power` reports the block as drawing nothing (see [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block)).

### Run a whole hierarchy

`rb pnr` with no run name runs every entry in the `pnr.yaml`, with each block's run before the runs that consume it:

- **Order.** Runs are sorted by their `blocks:` edges. Where the edges leave a choice, a run keeps its place in the file, so a `pnr.yaml` with no `blocks:` runs in file order as it always has.
- **Other files.** A block whose `pnr-path` is another `pnr.yaml` is pulled into the plan ahead of its consumer, and so are that block's own blocks. A pulled-in run writes to its own suite's `artefacts/`, takes that tree's lock, and its row in the results carries a `suite` key.
- **A failed block.** A run whose block failed, including an expected failure under `xfail`, is not attempted: neither published an abstract. It is reported as `FAIL` with `fail_stage: blocked`, a description naming the block, and a `blocked_by` list; an `xfail` marker does not excuse it. Runs that do not depend on the failed block still run, and a run is blocked through any number of levels.
- **A skipped run.** A block skipped by `-l` does not block its consumer, which uses the abstract already published, as a named run would. A run `-l` deselects is `SKIP` whatever its blocks did.
- **Configuration errors.** A cycle in `blocks:` (`pnr blocks: cycle: a -> b -> a`), a block naming a run its `pnr.yaml` does not define, a `pnr-path` that does not exist, or two runs that would write the same `artefacts/<run>` directory (two `pnr.yaml` files in one directory defining the same run name) stops the command before anything runs.

This orders P&R only. The top's synthesis reads the blocks' abstracts, so on a clean tree it has to run between the blocks' P&R and the top's.
