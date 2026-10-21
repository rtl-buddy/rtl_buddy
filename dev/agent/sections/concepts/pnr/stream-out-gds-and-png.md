## Stream out GDS and PNG

`--gds` streams the routed DEF to GDS with KLayout after a successful run; `--png` also renders it. Cell layouts come from the PDK's `cell-gds` and the run's `gds-paths` (put each macro's GDS there, its LEF in `lef-paths`). A missing input stops the export.

A cell in the DEF with no layout is written as an empty placeholder. `gds-mode` decides what that means:

- `preview` (default): the layout is kept, the run passes, and `gds_status: incomplete` and `gds_missing_cells` name the cells. A KLayout failure only warns.
- `strict`: any missing cell, missing KLayout or `klayout-tech`, or export failure fails the run with `fail_stage: export` and removes the GDS and PNG. The routed outputs stay.

List cells that are empty on purpose, such as an ORFS `fakeram45_*` macro, in `gds-allow-empty`.
