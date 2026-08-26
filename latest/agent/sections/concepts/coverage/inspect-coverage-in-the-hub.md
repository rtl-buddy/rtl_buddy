## Inspect coverage in the hub

Start the viewer service and open `/cov`:

```bash
rb hub start --serve-viewer
```

The pane shows totals, metric-ranked files, source annotations, individual points, and per-test attribution from the same model used by `rb cov`. Line selections can focus source and schematic views; module selections can focus the graph. See [Coverage pane](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/hub/#coverage-pane).

After `rb graph results`, the design graph also correlates declared `covers:` relationships with observed coverage and reports exercised, declared-only, and observed-but-undeclared items. See [Coverage on the graph](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/graph/#coverage-on-the-graph).
