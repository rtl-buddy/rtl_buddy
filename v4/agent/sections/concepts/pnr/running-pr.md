## Running P&R

```bash
# All runs in the default ./pnr.yaml
rb pnr

# A single run from a specific config
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml

# Reglvl-gated runs (1000 by default for tech-mapped flows)
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml -l 1000

# List runs without executing
rb pnr -c pnr/demo/pnr.yaml --list

# Stream out a routed GDS via KLayout after the run
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds

# Render a PNG of the GDS (implies --gds)
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png
```

### `--gds` and `--png`

When `--gds` is requested, `rb pnr` invokes KLayout headlessly after a
successful OpenROAD run, merging the routed DEF with the PDK's standard
cell GDS to produce `artefacts/<run>/<design>.gds`. `--png` additionally
renders a 2048×2048 PNG via the bundled `gds2png.py` helper. Both helpers
ship inside the wheel under `rtl_buddy/pnr/klayout/` and are copied into
the artefact dir at run time. KLayout failures emit `pnr.gds_failed` /
`pnr.png_failed` warnings but do not fail the P&R run — timing/DRC
metrics remain authoritative.
