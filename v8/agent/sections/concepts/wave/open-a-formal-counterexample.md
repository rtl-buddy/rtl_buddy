## Open a formal counterexample

```bash
uv run rb wave-fpv demo_fpv_counter_safety
```

`rb wave-fpv` reads `fpv.yaml`, finds the first counterexample trace under the verification's artefacts, and opens it in the configured Surfer entry. Use `-c` for another config and `--surfer <name>` to override the routing. There is no editor annotation, so mainline Surfer works.

It fails with a message if the verification has not run, passed without a counterexample, or produced no trace.
