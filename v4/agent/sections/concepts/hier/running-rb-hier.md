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
```

The model argument matches the `name:` of an entry in `models.yaml`. The runner uses that entry's filelist verbatim — same source of truth that `rb test`, `rb synth`, and `rb cdc` consume.
