## CDC and RDC annotations

`rb hier` can overlay clock-domain and reset-domain information when the corresponding analyzer pass has emitted a JSON map:

```bash
# Clock-domain overlay — colors each module by its primary clock
rtl-buddy-cdc --emit-domain-map -o clocks.json ...
rb hier demo_top --format dot --cdc-annotations clocks.json | dot -Tsvg -o hier.svg

# With a side legend mapping color → clock name (dot format only)
rb hier demo_top --format dot --cdc-annotations clocks.json --clock-legend | dot -Tsvg -o hier.svg

# Reset-domain overlay — colors each module by its primary reset
rtl-buddy-cdc --emit-reset-domain-map -o resets.json ...
rb hier demo_top --format dot --rdc-annotations resets.json | dot -Tsvg -o hier.svg
```

Both annotation files are JSON keyed by hierarchical instance path. `rb hier` validates that the files exist before invoking the renderer; the renderer's JSON contract (`schema_version`, `tool.*`, `design.top`, `nodes`, `edges`) is the integration boundary.

`--clock-legend` is honored only for `--format dot`; the tree and Mermaid renderers ignore it.
