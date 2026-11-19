## Stream out GDS and PNG

`--gds` streams the routed DEF to GDS with KLayout after a successful run; `--png` also renders an image. Cell layouts come from the PDK's `cell-gds` and the run's `gds-paths`, where each macro's GDS goes (its LEF goes in `lef-paths`). A missing input stops the export.

A cell in the DEF with no layout is written as an empty placeholder, and `gds-mode` decides what that means:

- `preview` (default): the layout is kept and the run passes, with `gds_status: incomplete` and `gds_missing_cells` naming the cells. A KLayout failure only warns.
- `strict`: a missing cell, missing KLayout or `klayout-tech`, or export failure fails the run (`fail_stage: export`) and removes the GDS and PNG. The routed outputs stay.

List cells that are empty on purpose, such as ORFS's `fakeram45_*` macros, in `gds-allow-empty`.
