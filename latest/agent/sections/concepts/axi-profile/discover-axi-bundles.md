## Discover AXI bundles

```bash
rb axi-profile discover soc_top
rb axi-profile discover soc_top -c design/soc_top/models.yaml
rb axi-profile discover soc_top -o /tmp/axi-bundles.yaml
```

rtl_buddy generates a stripped, deduplicated model filelist, then asks the profiler to discover bundles. Without `-o`, output goes to the model's `axi_bundles` path or, when unset, `artefacts/axi/<model>/axi-bundles.yaml`.

Commit the manifest so RTL-interface changes are reviewable. Discovery rewrites it in full; `--amend` does not merge prior manual edits.
