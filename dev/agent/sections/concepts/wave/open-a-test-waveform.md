## Open a test waveform

From a suite containing `tests.yaml`:

```bash
uv run rb wave basic
uv run rb wave basic --resim
```

If no waveform exists, the first command runs the test in debug mode. `--resim` always reruns it. RTL Buddy opens the newest supported FST or VCD under the test artefacts.

If `basic.surfer` exists beside `tests.yaml`, RTL Buddy passes it to Surfer as the initial signal layout.
