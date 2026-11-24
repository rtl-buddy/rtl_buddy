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

- Build blocks first. `rb pnr -c top/pnr.yaml --synth` builds every block, its synthesis and the top in order from a clean tree. `-j N` hardens independent blocks in parallel, each with its own `threads:`.
- A run whose block failed is not attempted (`fail_stage: blocked`, with `blocked_by`).
- The block must use the top's technology LEF and corner.
- An abstract is refused, naming what changed, when its inputs changed since hardening or a major rb version with different built-in flow steps or defaults hardened it. Re-harden it, or pass `--accept-stale`.
- A top whose blocks cover the core has no rows once rows are cut around them. With no standard cell left to place, rb skips global placement, resize, legalization, CTS and its repairs, the post-CTS and global-route timing repairs, antenna diode insertion and fill, routes the top-level nets, and qualifies the pass with `pnr.no_rows_skip_placement`. Clocks stay propagated, and the `after-timing-repair` hooks of a skipped repair do not run. A top with standard cells, such as port buffers (`buffer-ports`, on by default with `harden`), needs a strip of core rows for them; without one the floorplan stops with `no rows left for N standard cell(s)`.
- A block hardened with `abstract: {gds: false}` has no GDS. A parent's `strict` stream-out (its own `harden` and `rb pnr-export --gds-mode strict` included) fails at setup naming it. A `preview` stream-out, `--png` included, leaves it out of the layout and names it, as in [Stream out GDS and PNG](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#stream-out-gds-and-png).
