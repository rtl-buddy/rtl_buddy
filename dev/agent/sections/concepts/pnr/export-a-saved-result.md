## Export a saved result

`rb pnr-export` streams an existing routed result out again without rerunning OpenROAD, for example after installing KLayout:

```bash
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --png --gds-mode strict
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --checkpoint cts --png
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --png-only --lyp dark.lyp
```

`--checkpoint` exports a stage checkpoint into `checkpoints/<run-id>/export/`, marked not final. `--def` exports another DEF with the run's platform. `--png-only` re-renders the existing GDS. The export never touches the routed results. Its verdict is the export alone: anything not delivered is `FAIL` with `fail_stage: export`. `export.provenance.json` records what it read and wrote.
