## Resolve config paths

A relative path in YAML resolves from the file that declares it, never from `invocation_cwd`. Absolute paths pass through unchanged.

- Regression manifests resolve their suite and flow configs from the manifest directory.
- `tests.yaml` resolves testbench filelists, hook scripts and suite assets from the suite directory.
- `models.yaml` resolves model filelist entries from its own directory.
- `synth.yaml`, `fpv.yaml`, `pnr.yaml`, `power.yaml` and similar flow configs resolve their fields from their own directory.
