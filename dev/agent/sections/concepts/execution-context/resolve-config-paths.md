## Resolve config paths

Relative paths declared in YAML resolve from the file that owns them:

- Regression manifests resolve their listed suite or flow configs from the manifest directory.
- `tests.yaml` resolves testbench filelists, hook scripts, and suite assets from the suite directory.
- `models.yaml` resolves model filelist entries from its own directory.
- Flow configs such as `synth.yaml`, `fpv.yaml`, `pnr.yaml`, and `power.yaml` resolve their fields from their own directory.

Absolute paths pass through unchanged. A YAML path never changes meaning based on `invocation_cwd`.
