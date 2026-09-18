## Execution Contexts

Use explicit contexts, never ambient `os.getcwd()`:

- `invocation_cwd`: the directory where the user ran `rb`. Use it to resolve relative CLI arguments before they become absolute.
- `command_root`: the directory containing the command's primary config file.
- `suite_dir`: the command root for per-suite flows such as `tests.yaml`, `synth.yaml`, `fpv.yaml`, `pnr.yaml`, `power.yaml`, and `fpga.yaml`.
- `artifact_dir`: the generated workspace for one command item, normally `suite_dir/artefacts/<name>`.

Config-driven commands use their primary config's directory as `command_root`. Managed outputs go below it, external tools run from their artifact directory, and explicit CLI paths resolve from `invocation_cwd`.

`--run-tag <name>` namespaces one invocation's artefact root to `<command_root>/artefacts/.runs/<name>/`. It is accepted by `test`, `randtest`, `regression`, the dispatch job commands, and `graph results`; every per-run path in the table below moves below it, and so does the command's `rtl_buddy.log`. Shared builds (`artefacts/.shared-builds/`, keyed on the compile fingerprint) and `graph.json` are shared and must not be namespaced. A new path builder under `artefacts/` therefore has to be classified: per-run paths go through `run_artifact_root()` / `test_artifact_dir(..., run_tag=...)`, shared ones do not. Unset keeps today's flat layout byte for byte.
