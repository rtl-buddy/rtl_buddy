## Required dependencies

These are installed automatically when you `uv add rtl_buddy` — no action needed:

- `typer`, `click`, `pyserde[yaml]`, `ruamel.yaml`, `rich` — core CLI and config parsing.
- `pywellen` — FST/VCD waveform reader. Used by `rb wave` annotation regardless of which waveform viewer is configured; the data layer is viewer-independent, which is why it ships with the wheel rather than as a Surfer-side install step.
