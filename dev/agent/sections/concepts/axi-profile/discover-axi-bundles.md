## Discover AXI bundles

```bash
rb axi-profile discover soc_top
rb axi-profile discover soc_top -c design/soc_top/models.yaml
rb axi-profile discover soc_top -o /tmp/axi-bundles.yaml
```

rtl_buddy builds a stripped, deduplicated model filelist and asks the profiler to discover bundles. Output goes to `-o`, else the model's `axi_bundles` path, else `artefacts/axi/<model>/axi-bundles.yaml`.

Commit the manifest so RTL-interface changes are reviewable. Discovery rewrites it in full, and `--amend` does not preserve manual edits.
