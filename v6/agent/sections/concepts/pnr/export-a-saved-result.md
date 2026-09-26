## Export a saved result

KLayout, a PDK's GDS or the layer properties often arrive after the P&R does. `rb pnr-export` streams an existing routed result out again — DEF → GDS → PNG — without rerunning synthesis or OpenROAD:

```bash
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds-mode strict
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --def ../saved/demo_top.def
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --checkpoint cts --png
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png-only --lyp dark.lyp --png-width 4096 --png-height 4096
```

Run selection, `-c`, `-l` and `--gds-mode` mean what they mean for `rb pnr`, and the export reads the same configuration: the technology, the PDK's `cell-gds`, the run's `gds-paths` and `lef-paths`, and the `gds-allow-empty` list. The design's name comes from the upstream `synth:` entry, read out of `synth.yaml` — the export needs the synthesis *configuration* and none of its artefacts. `rb tool-check --required-for pnr-export` therefore asks for KLayout and for nothing else.

Nothing is launched before the saved result has been checked. The routed DEF must exist, be non-empty, and declare the design this run's synth entry names — its own `DESIGN <top> ;` statement is compared, so a result left by another design fails with both names rather than being streamed into a layout named after a design it does not contain. A missing layer properties file, a PDK with no `klayout-tech`, a configured input that is not on disk, and a missing KLayout each stop the export the same way. Nothing here is decided from file timestamps: a checkout, a copy or an archive restore rewrites those in any order.

The export clears **only what an export publishes** — the GDS, the PNG, `def2stream.report.json`, `def2stream.inputs.json` and its own record. The routed DEF, ODB, netlist, SDC and every P&R report stay exactly as they were, on a failed export as much as on a successful one.

`--def <path>` exports a DEF from elsewhere, with the platform and the top still coming from the run, and needs a single named run. `--checkpoint` exports a [stage checkpoint](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#keep-stage-checkpoints) instead of the routed result, and also needs a single named run: a stage name (`cts`, or `03_cts`) takes it from the `latest` run, `<run-id>/<stage>` from an older one, and a path names one of a checkpoint's files. The checkpoint must have a completed `checkpoint` event in its run's `progress.jsonl`, so a database a kill interrupted mid-write is refused. Everything the export writes — GDS, PNG, stream-out report, input manifest and record — goes to `checkpoints/<run-id>/export/<NN>_<stage>/`, never to the routed layout's paths; the row carries `checkpoint_stage`, `checkpoint_run_id` and `checkpoint_final: false`, its description names the checkpoint and says it is not final, and the export record gains a `checkpoint` block with the same `final`, `global_routed` and `congestion` labels as the manifest. `--checkpoint` and `--def` are exclusive. `--png-only` re-renders the PNG from the GDS already in the artefact directory: no stream-out, the GDS is an input and is never rewritten, and `--lyp`, `--png-width` and `--png-height` change how it is drawn. If a `def2stream.report.json` sits beside that GDS and says cells had no layout, the re-render carries the same qualifier; if no report sits beside it, nothing vouched for that layout, so the re-render reports it as qualified rather than complete.

### The export verdict

For `rb pnr` the export is a bonus over a P&R verdict, so in `preview` mode a failed export leaves the run passing. For `rb pnr-export` the export *is* the job:

- everything that was asked for, on disk — `PASS`, exit 0;
- a layout `preview` published with cells that have no GDS — `PASS` with the `GDS incomplete: …` qualifier, exit 0, exactly as in a `rb pnr` run;
- anything else, in either mode — `FAIL` with `fail_stage: export` and exit 1: no KLayout, no technology, an input off disk, a stream-out that wrote nothing, a `--png` render that failed, or, under `strict`, a layout with cells that have no GDS.

Rows carry the same fields a `rb pnr` row does — `gds_status`, `gds_mode`, `gds_missing_cells`, `gds_missing_cell_count`, `gds_allowed_empty_cells`, `gds_path`, `png_path` — plus `export_provenance`.

### The export record

Each export writes `export.provenance.json` into the artefact directory, whatever the outcome — a failure so early that the design cannot even be named writes none, and clears the previous one with the rest. It is the export's own document: it never edits `pnr.log`, `pnr.tcl`, the reports or the results of the P&R run it exports. A fresh `rb pnr` run clears it, because the run replaces the DEF the record describes.

```json
{
  "schema_version": 1,
  "generator": "rtl-buddy 6.53.0",
  "generated_at": "2026-09-21T15:04:12+08:00",
  "command": "pnr-export",
  "run": "demo_pnr_nangate45", "top": "demo_top",
  "gds_mode": "preview", "png_only": false,
  "tool": {"name": "klayout", "path": "/opt/homebrew/bin/klayout", "version": "KLayout 0.30.8"},
  "inputs": {
    "def": {"path": "…/demo_top.def", "size": 1415786, "sha256": "…"},
    "gds": null, "tech": "…/FreePDK45.lyt",
    "cell_gds": ["…"], "lef": ["…"], "missing": [], "allow_empty": ["fakeram45_*"]
  },
  "render": {"requested": true, "lyp": "…/FreePDK45.lyp", "width": 2048, "height": 2048},
  "outputs": {"gds": "…/demo_top.gds", "png": "…/demo_top.png"},
  "outcome": {"status": "complete", "delivered": true, "missing_cells": [], "allowed_empty_cells": [], "desc": ""}
}
```

Paths are project-relative POSIX where they can be, as in `phys-manifest.json`. Exactly one of `inputs.def` and `inputs.gds` is set — the DEF a stream-out read, or the GDS a re-render rendered — and it carries the size and the SHA-256 of the bytes that were read, which is what a later reader compares to decide whether the layout still belongs to the result beside it.
