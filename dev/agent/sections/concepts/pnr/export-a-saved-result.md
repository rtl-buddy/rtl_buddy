## Export a saved result

KLayout, a PDK's GDS or the layer properties often arrive after P&R does. `rb pnr-export` streams an existing routed result out again, DEF to GDS to PNG, without rerunning synthesis or OpenROAD:

```bash
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png --gds-mode strict
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --checkpoint cts --png
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png-only --lyp dark.lyp --png-width 4096 --png-height 4096
```

Run selection, `-c`, `-l` and `--gds-mode` work as for `rb pnr`, and the same stream-out inputs and `gds-allow-empty` apply. The export needs the `synth.yaml` entry (for the design name) but none of its artefacts. `rb tool-check --required-for pnr-export` checks for KLayout only.

- **`--def <path>`** exports a DEF from elsewhere, with platform and top taken from the run. It needs a single named run.
- **`--checkpoint`** exports a [stage checkpoint](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints) instead of the routed result. It needs a single named run and excludes `--def`. Give a stage (`cts` or `03_cts`) for the `latest` run, `<run-id>/<stage>` for an older one, or a path to a checkpoint file. Outputs go to `checkpoints/<run-id>/export/<NN>_<stage>/` and are marked not final.
- **`--png-only`** re-renders the PNG from the GDS already in the artefact directory, without stream-out. `--lyp`, `--png-width` and `--png-height` change how it is drawn.

Before launching, the export checks that the routed DEF exists, is non-empty and declares the design the synth entry names; a mismatch fails with both names. A missing layer properties file, `klayout-tech`, input or KLayout stops it the same way. It clears only its own outputs (GDS, PNG, `def2stream.*`, `export.provenance.json`) and never touches the routed results, even when it fails.

The export is the whole verdict here. Everything requested on disk is `PASS` (exit 0), including a `preview` layout with the `GDS incomplete: …` qualifier. Anything else, in either mode, is `FAIL` with `fail_stage: export` and exit 1. `export.provenance.json` records the tool, the input DEF or GDS with its SHA-256, the render options and the outcome.
