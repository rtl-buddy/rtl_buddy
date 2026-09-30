## Stream-out completeness

A cell that the DEF instantiates but no GDS contains is streamed as an empty placeholder. Sometimes that is intended, such as an ORFS `fakeram45` macro that exists only in LEF. Sometimes a real SRAM GDS was forgotten. `gds-mode` says which the run means:

```yaml
runs:
  - name: demo_pnr_signoff
    # ...
    gds-mode: strict          # default: preview
    gds-allow-empty:          # cells that are empty on purpose
      - fakeram45_*
```

- **`preview`** (default) keeps the layout and reports what is missing: a `pnr.gds_incomplete` warning naming every cell, `GDS incomplete: …` in the run description, and `gds+png (incomplete: N missing)` in the summary. `--machine` output has `gds_status: incomplete` and `gds_missing_cells`. The run still passes.
- **`strict`** refuses to publish an incomplete layout. Cells with no layout, a missing KLayout, a PDK with no `klayout-tech`, a missing input, any stream-out failure and a failed `--png` render each make the export a `FAIL` with `fail_stage: export`. The GDS, PNG and stream-out report are removed; the routed outputs stay. `xfail:` does not excuse an export failure.

`gds-allow-empty` takes cell names or case-sensitive `fnmatch` globs. A covered cell is not missing in either mode and is counted as `(N empty by design)` in the summary.
