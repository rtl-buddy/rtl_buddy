## Open a test waveform

Run from a suite that contains `tests.yaml`:

```bash
uv run rb wave basic
uv run rb wave basic --resim
```

`rb wave` opens the newest supported FST or VCD under the test's artefacts. If none exists it first runs the test in debug mode; `--resim` always reruns the test. If `basic.surfer` exists beside `tests.yaml`, it is passed to Surfer as the initial signal layout.
