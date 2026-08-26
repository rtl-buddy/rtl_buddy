## Render a testbench hierarchy

Use TB view when you need the hierarchy above and around the DUT:

```bash
rb hier basic_traffic --view tb
```

In this mode the positional name selects a test from `tests.yaml`, not a model. The test identifies the DUT model and testbench top. Tests that share the same `(model, testbench)` reuse the generated hierarchy artefact.
