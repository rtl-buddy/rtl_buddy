## Running `rb hier`

```bash
# ASCII tree (default)
rb hier demo_top

# Save Mermaid source to a file
rb hier demo_top --format mermaid -o demo_top.mmd

# DOT → SVG via Graphviz
rb hier demo_top --format dot | dot -Tsvg -o demo_top.svg

# JSON export for downstream tooling
rb hier demo_top --format json -o demo_top.hier.json

# Point at a non-default models.yaml
rb hier demo_top -c design/demo_top/models.yaml

# Pin a renderer build
rb hier demo_top --tool /opt/rtl-buddy-view/bin/rtl-buddy-view

# TB-rooted view — render the testbench for a test, DUT called out as subtree
rb hier basic_traffic --view tb
```

The model argument matches the `name:` of an entry in `models.yaml`. The runner uses that entry's filelist verbatim — same source of truth that `rb test`, `rb synth`, and `rb cdc` consume. In `--view tb` mode the positional argument is a test name from `tests.yaml` instead; the test pins both the model (DUT side) and the testbench top, and the renderer merges the model + TB filelists before elaborating from `--tb-top`.
