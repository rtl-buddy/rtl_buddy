## Render a testbench hierarchy

`--view tb` shows the hierarchy above and around the DUT:

```bash
rb hier basic_traffic --view tb
```

Here the positional name is a test from `tests.yaml`, not a model. The test supplies the DUT model and testbench top. Tests with the same `(model, testbench)` reuse one generated hierarchy artefact.
