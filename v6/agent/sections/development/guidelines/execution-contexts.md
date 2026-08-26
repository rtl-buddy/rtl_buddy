## Execution Contexts

Use explicit contexts, never ambient `os.getcwd()`:

- `invocation_cwd`: the directory where the user ran `rb`. Use it to resolve relative CLI arguments before they become absolute.
- `command_root`: the directory containing the command's primary config file.
- `suite_dir`: the command root for per-suite flows such as `tests.yaml`, `synth.yaml`, `fpv.yaml`, `pnr.yaml`, `power.yaml`, and `fpga.yaml`.
- `artifact_dir`: the generated workspace for one command item, normally `suite_dir/artefacts/<name>`.

Config-driven commands use their primary config's directory as `command_root`. Managed outputs go below it, external tools run from their artifact directory, and explicit CLI paths resolve from `invocation_cwd`.
