## Execution Contexts

Keep command execution rooted in explicit contexts rather than ambient `os.getcwd()`:

- `invocation_cwd`: the directory where the user ran `rb`. Use it to resolve relative CLI arguments before they become absolute.
- `command_root`: the directory containing the command's primary config file.
- `suite_dir`: the command root for per-suite flows such as `tests.yaml`, `synth.yaml`, `cdc.yaml`, `fpv.yaml`, `pnr.yaml`, and `power.yaml`.
- `artifact_dir`: the generated workspace for one command item, normally `suite_dir/artefacts/<name>`.

Config-driven commands should be rooted at their primary config file.
Generated artifacts should go under the command root.
External tools should run from their artifact directory.
Explicit CLI input and output paths should remain relative to `invocation_cwd`, matching normal shell behavior.
