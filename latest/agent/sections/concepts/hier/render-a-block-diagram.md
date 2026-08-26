## Render a block diagram

For sibling dataflow instead of an instantiation tree, generate block-diagram DOT:

```bash
rb hier demo_top --format dot --block-diagram | dot -Tsvg -o demo_top_block.svg
```

This requires `rtl-buddy-sch >= 0.8.0`. Older renderers fail with an upgrade instruction. The option is meaningful only with DOT output.
