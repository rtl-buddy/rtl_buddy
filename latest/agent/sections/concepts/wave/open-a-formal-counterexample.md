## Open a formal counterexample

```bash
uv run rb wave-fpv demo_fpv_counter_safety
```

`rb wave-fpv` reads `fpv.yaml`, finds the first counterexample trace under the verification's artefacts, and opens it in the configured Surfer entry. Use `-c` for another config or `--surfer <name>` to override routing.

This command does not enable the editor annotation round trip, so mainline Surfer is sufficient. It fails with a clear message when the verification has not run, passed without a counterexample, or produced no trace.
