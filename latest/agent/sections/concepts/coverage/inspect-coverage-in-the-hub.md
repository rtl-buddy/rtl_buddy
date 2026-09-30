## Inspect coverage in the hub

```bash
rb hub start --serve-viewer
```

Open `/cov`. The pane shows totals, ranked files, source annotations, points and per-test attribution from the same model as `rb cov`. Its figures are per elaboration; the source-point percentages are in the header tooltip. Line selections focus the source and schematic views, and module selections focus the graph. See [Coverage pane](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/hub/#coverage-pane).

After `rb graph results`, the design graph also joins declared `covers:` relationships to observed coverage; see [Coverage on the graph](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/graph/#coverage-on-the-graph).
