## Select a parser and annotations

Pass `--frontend slang` for SystemVerilog the default parser cannot elaborate. The renderer validates frontend names.

Domain overlays are JSON maps keyed by hierarchical instance path:

```bash
rb hier demo_top --format dot --clock-legend | dot -Tsvg -o clocks.svg
rb hier demo_top --format dot --rdc-annotations resets.json | dot -Tsvg -o resets.svg
```

rtl_buddy checks that annotation files exist before starting the renderer. `--clock-legend` applies only to DOT output.
