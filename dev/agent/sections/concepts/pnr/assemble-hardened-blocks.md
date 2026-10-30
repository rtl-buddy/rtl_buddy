## Assemble hardened blocks

A top run names its blocks under `blocks:`, in `pnr.yaml` and in the `synth.yaml` entry it reads. The top's filelist must leave the module a blackbox (a port-only stub).

```yaml
# pnr.yaml
    blocks:
      - {name: alu_block, pnr: alu_block_pnr, pnr-path: ../alu/pnr.yaml}
# synth.yaml (pnr-path required, relative to synth.yaml)
    blocks:
      - {name: alu_block, pnr: alu_block_pnr, pnr-path: ../../pnr/alu/pnr.yaml}
```

- Build blocks first. `rb pnr -c top/pnr.yaml --synth` runs every block, its synthesis and the top in order from a clean tree; `-j N` hardens independent blocks in parallel. Each run uses its own `threads:`.
- A run whose block failed is not attempted (`fail_stage: blocked`, with `blocked_by`).
- The block must use the same technology LEF and corner as the top.
- An abstract whose inputs changed since hardening, or that a major version of rb with different built-in flow steps or defaults hardened, is refused, naming what changed. Re-harden it, or pass `--accept-stale`.
- A block hardened with `abstract: {gds: false}` has no GDS. A `gds-mode: strict` stream-out of the parent, including the parent's own `harden` and `rb pnr-export --gds-mode strict`, fails at setup naming the block. A `preview` stream-out, `--png` included, treats the block as a cell without GDS: the layout lacks it and the result names it, as in [Stream out GDS and PNG](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stream-out-gds-and-png).
