## Path Ownership

Resolve config-owned paths from the config file that owns them:

- `root_config.yaml` is discovered from the command root for config-driven commands.
- `regression.yaml` resolves listed suite configs relative to itself.
- `tests.yaml` resolves testbench filelists, hook script paths, and suite-local runtime assets relative to the suite directory.
- `models.yaml` resolves model filelist entries relative to the `models.yaml` file that defined them.
- `synth.yaml`, `cdc.yaml`, `fpv.yaml`, `pnr.yaml`, and `power.yaml` resolve their own fields relative to their config directory.

Do not let relative paths silently depend on where the user happened to invoke the command.
If a path is passed to an external tool, prefer an absolute path unless the value is intentionally artifact-relative.
