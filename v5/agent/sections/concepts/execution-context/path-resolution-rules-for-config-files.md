## Path resolution rules for config files

`rtl_buddy` resolves config-owned paths from the config file that owns them:

- `regression.yaml` resolves listed suite configs relative to itself.
- `tests.yaml` resolves testbench filelists, hook script paths, and suite-local assets relative to the suite directory.
- `models.yaml` resolves model filelist entries relative to the `models.yaml` file that declared them.
- `synth.yaml`, `cdc.yaml`, `fpv.yaml`, `pnr.yaml`, `power.yaml` resolve their own fields relative to their config directory.

A relative path inside a YAML file never depends on where you ran `rb`. Absolute paths pass through unchanged.
