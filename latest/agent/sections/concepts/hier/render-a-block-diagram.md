## Render a block diagram

For sibling dataflow instead of an instantiation tree, generate block-diagram DOT:

```bash
rb hier demo_top --format dot --block-diagram | dot -Tsvg -o demo_top_block.svg
```

This needs `rtl-buddy-sch >= 0.8.0`; older renderers fail with an upgrade message. It applies only to DOT output.
