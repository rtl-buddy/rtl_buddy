## Trim a figure for a document

Three options strip detail from a DOT or Mermaid figure:

```bash
rb hier demo_top --format dot --block-diagram --params none --prune-flags | dot -Tsvg -o demo_top.svg
rb hier demo_top --format mermaid --clock-colors clock-colors.json -o demo_top.mmd
```

- `--params none` drops the parameter rows from every instance box, overriding `// rbsch: params=` pragmas. `--params all`, the default, shows every override the pragmas allow.
- `--prune-flags` drops root-port flags that no edge lands on, typically clock and reset ports. It works only with `--format dot --block-diagram`.
- `--clock-colors` pins a colour to named clocks of the clock-domain annotations, so a clock keeps its colour across a set of figures. Other clocks get the remaining swatches. The file is:

```json
{"schema_version": "1.0", "clocks": {"clk_a": "blue", "clk_b": "#dcfce7"}}
```

A colour is a swatch name (`blue`, `green`, `yellow`, `pink`, `purple`, `orange`, `cyan`) or `#rrggbb`. To keep a colour with the RTL instead, put a `// rbsch: clock color=blue` pragma on the clock port; the file wins where both name a clock.

These options need `rtl-buddy-sch >= 0.12.0`; older renderers fail with an upgrade message. rtl_buddy refuses a combination the renderer would ignore: `--params` or `--clock-colors` with `tree` or `json` output, `--prune-flags` without a block diagram, and `--clock-colors` without clock-domain annotations.
